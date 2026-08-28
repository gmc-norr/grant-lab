# Side-by-Side Version Compare Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render the History tab's two-version comparison as two aligned columns — older left, newer right — with a toggle back to today's stacked view.

**Architecture:** The run-walk that `refineRuns` already performs is extracted into one internal `blocks()` function. `refineRuns` is re-expressed over it with no change to its signature or output, and a new `pairRows()` uses the same walk to emit `{left, right}` pairs. A new `SideBySideDiffView` renders those pairs as a CSS grid inside one scroll container, so a pair is literally one grid row and the columns cannot drift.

**Tech Stack:** React 18, TypeScript, Vite, Mantine, vitest + @testing-library/react, CSS modules. Playwright (for the Task 6 browser gate only).

**Spec:** `docs/superpowers/specs/2026-08-28-side-by-side-diff-design.md` rev 2 (`f3cb082c`).

## Global Constraints

- **Work from `frontend/`** for every frontend command. `cd frontend` persists inside one Bash call but NOT across calls — re-anchor every time.
- Baseline measured 2026-08-28 by running the suite: **113 files, 658 tests passing**.
- Frontend gates, all must be clean: `pnpm exec tsc --noEmit`, `pnpm vitest run`, `pnpm build`.
- **Judge vitest on the printed file and test counts, not on the absence of red.** `include` is `src/**/*.{test,spec}.{ts,tsx}` — a misnamed or misplaced test file is silently uncollected and reports as success. Baseline before this plan: **113 files, 658 tests passing**. Every task must state the new counts.
- `pnpm add` is refused by a 7-day `minimumReleaseAge` cooldown. **This plan adds no dependency.** If you believe you need one, stop and escalate.
- `DiffView.tsx`, `ProposalReview.tsx`, and `InlineProposalReview.tsx` are **not modified by any task**. Their tests must pass unmodified.
- The 16 existing cases in `diff.test.ts` (4) and `diff.refine.test.ts` (12) must pass **unmodified**. Editing one to accommodate a change is a failed task, not a fix.
- Colour values are exact, from spec §5.2: del row `rgba(248, 81, 73, 0.15)`, add row `rgba(46, 160, 67, 0.15)`, del span `rgba(248, 81, 73, 0.4)` plus strikethrough, add span `rgba(46, 160, 67, 0.4)`.
- URL parameter is exactly `layout`, values exactly `split` and `unified`. Default and fallback for any unrecognised value is `split`.
- **Playwright's package link is broken in this repo.** `frontend/node_modules/playwright` is a dangling symlink; `require.resolve('playwright')` fails. Import from `frontend/node_modules/.pnpm/playwright@1.60.0/node_modules/playwright/index.mjs` instead. Only Task 6 needs this.
- Do not add `committed_at` (or any field) to the backend. `VersionContent` stays `{commit_sha, artifact, markdown}`. The header timestamp comes from the version **list** row.

---

## File Structure

**Modify**

| File | Responsibility after this plan |
|---|---|
| `frontend/src/cockpit/diff.ts` | Pure diff algebra: line diff, word refinement, the shared `blocks()` walk, and `pairRows()` |
| `frontend/src/cockpit/historyNav.ts` | The History tab's URL state, now including `layout` |
| `frontend/src/cockpit/HistoryPanel.tsx` | The tab: list, selection, direction, and the choice of renderer |

**Create**

| File | Responsibility |
|---|---|
| `frontend/src/cockpit/SideBySideDiffView.tsx` | Renders `DiffPair[]` as a two-column grid with a sticky header |
| `frontend/src/cockpit/SideBySideDiffView.module.css` | That component's grid, colours, and sticky rules |
| `frontend/src/cockpit/diff.pair.test.ts` | `pairRows` rules, and the equivalence + invariant property tests |
| `frontend/src/cockpit/SideBySideDiffView.test.tsx` | Component rendering |

**Unchanged, but their tests must stay green:** `DiffView.tsx`, `ProposalReview.tsx`, `InlineProposalReview.tsx`, `diff.test.ts`, `diff.refine.test.ts`, `DiffView.test.tsx`.

---

### Task 1: The shared `blocks()` walk

`refineRuns` currently does its own inline scan for runs of `del` followed by runs of `add`. Task 2 needs the identical scan. Extract it once so the two cannot drift apart, and prove the extraction changed nothing.

**Files:**
- Modify: `frontend/src/cockpit/diff.ts` (the `refineRuns` function, currently lines 127–159)
- Create: `frontend/src/cockpit/diff.pair.test.ts`

**Interfaces:**
- Consumes: `DiffRow`, `wordDiff`, `withinWordCap` — all already in `diff.ts`.
- Produces: a module-internal `blocks(rows: DiffRow[]): Block[]` and the `Block` type. **Not exported** — Task 2 uses it from inside the same module.

- [ ] **Step 1: Write the failing equivalence test**

Create `frontend/src/cockpit/diff.pair.test.ts`:

```ts
import { describe, expect, it } from "vitest";

import { type DiffRow, lineDiff, refineRuns, withinWordCap, wordDiff } from "./diff";

/** The refineRuns implementation as it stood BEFORE the blocks() extraction,
 *  copied verbatim from diff.ts at commit f3cb082c. It exists only to prove the
 *  extraction changed nothing, over generated input rather than the 16
 *  hand-picked cases in diff.test.ts and diff.refine.test.ts. */
function refineRunsBefore(rows: DiffRow[]): DiffRow[] {
  const out: DiffRow[] = [];
  let i = 0;
  while (i < rows.length) {
    if (rows[i].tag !== "del") {
      out.push(rows[i]);
      i++;
      continue;
    }
    const delStart = i;
    while (i < rows.length && rows[i].tag === "del") i++;
    const addStart = i;
    while (i < rows.length && rows[i].tag === "add") i++;
    const dels = rows.slice(delStart, addStart);
    const adds = rows.slice(addStart, i);
    const paired = Math.min(dels.length, adds.length);
    const refined = dels
      .slice(0, paired)
      .map((d, k) => (withinWordCap(d.text, adds[k].text) ? wordDiff(d.text, adds[k].text) : null));
    for (let k = 0; k < dels.length; k++) {
      const r = k < paired ? refined[k] : null;
      out.push(r ? { ...dels[k], spans: r.del } : dels[k]);
    }
    for (let k = 0; k < adds.length; k++) {
      const r = k < paired ? refined[k] : null;
      out.push(r ? { ...adds[k], spans: r.add } : adds[k]);
    }
  }
  return out;
}

/** Seeded PRNG so a failure reproduces exactly. Deliberately not Math.random. */
function mulberry32(seed: number): () => number {
  return () => {
    seed |= 0;
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** A tiny alphabet is the point: with four words and short documents, runs of
 *  every shape actually occur — pure insertions, pure deletions, and runs
 *  longer on one side than the other. A large alphabet would make almost every
 *  line unique and exercise only one branch. */
function randomDoc(rand: () => number): string {
  const words = ["alpha", "bravo", "charlie", "delta"];
  const lineCount = 1 + Math.floor(rand() * 8);
  const lines: string[] = [];
  for (let i = 0; i < lineCount; i++) {
    const wordCount = 1 + Math.floor(rand() * 3);
    const line: string[] = [];
    for (let w = 0; w < wordCount; w++) line.push(words[Math.floor(rand() * words.length)]);
    lines.push(line.join(" "));
  }
  return lines.join("\n");
}

describe("the blocks() extraction is inert", () => {
  it("matches the pre-refactor refineRuns over 300 generated document pairs", () => {
    const rand = mulberry32(20260828);
    for (let n = 0; n < 300; n++) {
      const a = randomDoc(rand);
      const b = randomDoc(rand);
      const rows = lineDiff(a, b);
      expect(refineRuns(rows)).toEqual(refineRunsBefore(rows));
    }
  });

  it("covers every block shape across those documents", () => {
    // A guard on the generator itself: if it stopped producing insertions or
    // deletions, the test above would still pass while proving much less.
    const rand = mulberry32(20260828);
    const seen = new Set<string>();
    for (let n = 0; n < 300; n++) {
      const rows = lineDiff(randomDoc(rand), randomDoc(rand));
      for (let i = 0; i < rows.length; i++) {
        if (rows[i].tag === "del") seen.add(rows[i + 1]?.tag === "add" ? "replace" : "delete");
        else if (rows[i].tag === "add" && rows[i - 1]?.tag !== "del") seen.add("insert");
        else if (rows[i].tag === "same") seen.add("same");
      }
    }
    expect([...seen].sort()).toEqual(["delete", "insert", "replace", "same"]);
  });
});
```

- [ ] **Step 2: Run it and watch it pass — this is the BEFORE baseline**

Run: `cd frontend && pnpm vitest run src/cockpit/diff.pair.test.ts`

Expected: **2 passed**. It passes because nothing has changed yet — `refineRuns` and `refineRunsBefore` are the same code. That is the point: it establishes the baseline the refactor must preserve. If the second test fails here, the generator is not producing all four block shapes and must be fixed before continuing.

- [ ] **Step 3: Extract `blocks()` and re-express `refineRuns`**

In `frontend/src/cockpit/diff.ts`, replace the whole `refineRuns` function (currently starting at `export function refineRuns(rows: DiffRow[]): DiffRow[] {`) with:

```ts
type Block =
  | { kind: "single"; row: DiffRow }
  | { kind: "change"; dels: DiffRow[]; adds: DiffRow[] };

/** Group a flat diff into single rows and change-blocks. This is the walk
 *  refineRuns has always performed, lifted out so pairRows uses the SAME one.
 *
 *  A `del` row OPENS a change-block: the contiguous run of `del` rows, then the
 *  contiguous run of `add` rows immediately following it. Any other row is a
 *  `single`.
 *
 *  Two consequences that are easy to get wrong: `dels` is NEVER empty in a
 *  change-block, because only a `del` opens one. A run of `add` rows with no
 *  `del` before it is NOT a change-block — each such row arrives as its own
 *  `single`. */
function blocks(rows: DiffRow[]): Block[] {
  const out: Block[] = [];
  let i = 0;
  while (i < rows.length) {
    if (rows[i].tag !== "del") {
      out.push({ kind: "single", row: rows[i] });
      i++;
      continue;
    }
    const delStart = i;
    while (i < rows.length && rows[i].tag === "del") i++;
    const addStart = i;
    while (i < rows.length && rows[i].tag === "add") i++;
    out.push({ kind: "change", dels: rows.slice(delStart, addStart), adds: rows.slice(addStart, i) });
  }
  return out;
}

export function refineRuns(rows: DiffRow[]): DiffRow[] {
  const out: DiffRow[] = [];
  for (const b of blocks(rows)) {
    if (b.kind === "single") {
      out.push(b.row);
      continue;
    }
    const { dels, adds } = b;
    const paired = Math.min(dels.length, adds.length);
    // One wordDiff per pair, not two: it used to be computed twice, once for
    // `.del` and once for `.add`, doubling the cost of the matrix capped below.
    // A pair over the cap yields `null` and stays two whole-line rows.
    const refined = dels
      .slice(0, paired)
      .map((d, k) => (withinWordCap(d.text, adds[k].text) ? wordDiff(d.text, adds[k].text) : null));
    for (let k = 0; k < dels.length; k++) {
      const r = k < paired ? refined[k] : null;
      out.push(r ? { ...dels[k], spans: r.del } : dels[k]);
    }
    for (let k = 0; k < adds.length; k++) {
      const r = k < paired ? refined[k] : null;
      out.push(r ? { ...adds[k], spans: r.add } : adds[k]);
    }
  }
  return out;
}
```

Keep the existing block comment above `refineRuns` (the one explaining that it pairs contiguous RUNS by index, not adjacent rows) exactly where it is.

- [ ] **Step 4: Prove the refactor is inert**

Run: `cd frontend && pnpm vitest run src/cockpit/diff.pair.test.ts src/cockpit/diff.test.ts src/cockpit/diff.refine.test.ts`

Expected: **3 files, 18 tests passed** (2 new + 4 + 12). Zero edits to `diff.test.ts` or `diff.refine.test.ts`.

- [ ] **Step 5: Prove the extraction is load-bearing (mutation check)**

Temporarily break `blocks()` — change `while (i < rows.length && rows[i].tag === "add") i++;` to `if (i < rows.length && rows[i].tag === "add") i++;` — and re-run Step 4's command.

Expected: the equivalence test **FAILS**. A test that cannot fail on a real defect proves nothing. Revert the mutation and confirm green again before committing.

- [ ] **Step 6: Typecheck and commit**

```bash
cd frontend && pnpm exec tsc --noEmit
cd /home/parlar_ai/dev/call-monitor
git add frontend/src/cockpit/diff.ts frontend/src/cockpit/diff.pair.test.ts
git commit -m "refactor(diff): extract the run-walk both refineRuns and pairRows need

refineRuns' inline scan for a del-run followed by an add-run is lifted into
blocks(). Signature and output unchanged; the 16 existing cases pass
unmodified. Equivalence over every DiffRow[] is carried by a property test
against the pre-refactor implementation, kept in the test file, over 300
generated document pairs from a four-word alphabet so all four block shapes
actually occur.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: `pairRows()`

Turn the flat refined diff into left/right pairs, using the same `blocks()` walk so the columns and the word highlighting can never describe different edits.

**Files:**
- Modify: `frontend/src/cockpit/diff.ts` (append after `refineRuns`)
- Modify: `frontend/src/cockpit/diff.pair.test.ts` (append)

**Interfaces:**
- Consumes: `blocks()` from Task 1 (module-internal), `DiffRow`.
- Produces: `export type DiffPair = { left: DiffRow | null; right: DiffRow | null }` and `export function pairRows(rows: DiffRow[]): DiffPair[]`. Task 3 renders `DiffPair[]`.

- [ ] **Step 1: Write the failing tests**

Append to `frontend/src/cockpit/diff.pair.test.ts` (and add `pairRows` to the existing import from `./diff`):

```ts
describe("pairRows", () => {
  it("puts an unchanged line on both sides", () => {
    const pairs = pairRows([{ tag: "same", text: "alpha" }]);
    expect(pairs).toEqual([
      { left: { tag: "same", text: "alpha" }, right: { tag: "same", text: "alpha" } },
    ]);
  });

  it("puts a rewritten line's old text left and new text right, on one pair", () => {
    const pairs = pairRows(refineRuns(lineDiff("recruit 40 patients", "recruit 120 patients")));
    expect(pairs).toHaveLength(1);
    expect(pairs[0].left?.text).toBe("recruit 40 patients");
    expect(pairs[0].right?.text).toBe("recruit 120 patients");
  });

  it("carries the word spans through untouched", () => {
    const pairs = pairRows(refineRuns(lineDiff("recruit 40 patients", "recruit 120 patients")));
    expect(pairs[0].left?.spans?.filter((s) => s.tag === "del").map((s) => s.text)).toEqual(["40"]);
    expect(pairs[0].right?.spans?.filter((s) => s.tag === "add").map((s) => s.text)).toEqual(["120"]);
  });

  it("leaves the right side empty for a deletion with no partner", () => {
    // Three deletions, one addition: dels[0] pairs; dels[1] and dels[2] do not.
    const pairs = pairRows([
      { tag: "del", text: "one" },
      { tag: "del", text: "two" },
      { tag: "del", text: "three" },
      { tag: "add", text: "ONE" },
    ]);
    expect(pairs).toEqual([
      { left: { tag: "del", text: "one" }, right: { tag: "add", text: "ONE" } },
      { left: { tag: "del", text: "two" }, right: null },
      { left: { tag: "del", text: "three" }, right: null },
    ]);
  });

  it("leaves the left side empty for an addition with no partner", () => {
    const pairs = pairRows([
      { tag: "del", text: "one" },
      { tag: "add", text: "ONE" },
      { tag: "add", text: "TWO" },
    ]);
    expect(pairs).toEqual([
      { left: { tag: "del", text: "one" }, right: { tag: "add", text: "ONE" } },
      { left: null, right: { tag: "add", text: "TWO" } },
    ]);
  });

  it("treats a pure insertion as right-side-only, not as a rewrite", () => {
    // No `del` precedes these adds, so they are singles, not a change-block.
    const pairs = pairRows(refineRuns(lineDiff("alpha\ndelta", "alpha\nbravo\ncharlie\ndelta")));
    expect(pairs.map((p) => [p.left?.text ?? null, p.right?.text ?? null])).toEqual([
      ["alpha", "alpha"],
      [null, "bravo"],
      [null, "charlie"],
      ["delta", "delta"],
    ]);
  });

  it("returns nothing for an empty diff", () => {
    expect(pairRows([])).toEqual([]);
  });
});

describe("the pairing invariant from spec section 4.1", () => {
  it("never word-highlights a row it did not place opposite a partner", () => {
    const rand = mulberry32(19700101);
    for (let n = 0; n < 300; n++) {
      const refined = refineRuns(lineDiff(randomDoc(rand), randomDoc(rand)));
      const pairs = pairRows(refined);
      for (const row of refined) {
        if (!row.spans) continue;
        // Identity, not equality: pairRows passes the same objects through.
        const pair = pairs.find((p) => p.left === row || p.right === row);
        expect(pair, `a spanned row was not in any pair: ${JSON.stringify(row)}`).toBeDefined();
        expect(pair!.left).not.toBeNull();
        expect(pair!.right).not.toBeNull();
      }
    }
  });

  it("accounts for every row exactly once", () => {
    const rand = mulberry32(19700102);
    for (let n = 0; n < 100; n++) {
      const refined = refineRuns(lineDiff(randomDoc(rand), randomDoc(rand)));
      const pairs = pairRows(refined);
      const placed: DiffRow[] = [];
      for (const p of pairs) {
        if (p.left) placed.push(p.left);
        // A `same` row is the SAME object on both sides — count it once.
        if (p.right && p.right !== p.left) placed.push(p.right);
      }
      expect(placed).toHaveLength(refined.length);
      for (const row of refined) expect(placed).toContain(row);
    }
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && pnpm vitest run src/cockpit/diff.pair.test.ts`

Expected: FAIL — `pairRows is not a function` / TypeScript cannot resolve the import.

- [ ] **Step 3: Implement `pairRows`**

Append to `frontend/src/cockpit/diff.ts`, immediately after `refineRuns`:

```ts
// ---------------------------------------------------------------------------
// Side-by-side pairing.
//
// Takes the OUTPUT of refineRuns, so `spans` are already present and pass
// through untouched. Uses the same blocks() walk, so a pair placed opposite
// here is exactly a pair refineRuns considered for word refinement.
//
// The converse does NOT hold and must not be assumed: withinWordCap can skip
// refinement for an over-cap pair, which is still placed opposite and simply
// renders whole-line. Spec section 4.1.
// ---------------------------------------------------------------------------

export type DiffPair = { left: DiffRow | null; right: DiffRow | null };

export function pairRows(rows: DiffRow[]): DiffPair[] {
  const out: DiffPair[] = [];
  for (const b of blocks(rows)) {
    if (b.kind === "single") {
      const row = b.row;
      // A `del` cannot reach here — it always opens a change-block — but
      // handling it keeps the function total rather than relying on that.
      if (row.tag === "add") out.push({ left: null, right: row });
      else if (row.tag === "del") out.push({ left: row, right: null });
      else out.push({ left: row, right: row });
      continue;
    }
    const { dels, adds } = b;
    const paired = Math.min(dels.length, adds.length);
    for (let k = 0; k < paired; k++) out.push({ left: dels[k], right: adds[k] });
    // Leftover dels before leftover adds, matching the order refineRuns pushes
    // them in, so both layouts tell the edit in the same sequence.
    for (let k = paired; k < dels.length; k++) out.push({ left: dels[k], right: null });
    for (let k = paired; k < adds.length; k++) out.push({ left: null, right: adds[k] });
  }
  return out;
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd frontend && pnpm vitest run src/cockpit/diff.pair.test.ts`

Expected: **11 tests passed** (2 from Task 1 + 7 rule tests + 2 invariant tests).

- [ ] **Step 5: Prove the invariant test can fail**

Temporarily change `for (let k = paired; k < dels.length; k++) out.push({ left: dels[k], right: null });` to `... out.push({ left: dels[k], right: dels[k] });` and re-run.

Expected: the "accounts for every row exactly once" test **FAILS**. Revert and confirm green.

- [ ] **Step 6: Typecheck and commit**

```bash
cd frontend && pnpm exec tsc --noEmit
cd /home/parlar_ai/dev/call-monitor
git add frontend/src/cockpit/diff.ts frontend/src/cockpit/diff.pair.test.ts
git commit -m "feat(diff): pairRows — left/right pairs from the shared run-walk

Same blocks() walk as refineRuns, so a pair placed opposite is exactly a
pair considered for word refinement. The converse does not hold and is
tested one-directionally: withinWordCap can skip an over-cap pair, which is
still opposite and simply renders whole-line.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: `SideBySideDiffView`

Render `DiffPair[]` as a two-column grid inside one scroll container, with a sticky header naming each side.

**Files:**
- Create: `frontend/src/cockpit/SideBySideDiffView.tsx`
- Create: `frontend/src/cockpit/SideBySideDiffView.module.css`
- Create: `frontend/src/cockpit/SideBySideDiffView.test.tsx`

**Interfaces:**
- Consumes: `DiffPair` and `DiffRow` from `./diff` (Task 2).
- Produces: `export function SideBySideDiffView(props: { pairs: DiffPair[]; olderLabel: string; newerLabel: string }): JSX.Element`. Task 5 renders it.

**Scroll container decision:** this component owns its scroller (`max-height: 60vh; overflow: auto`), rather than relying on the cockpit's `.editorScroll`. Two reasons: `position: sticky` then resolves against an element this component controls, and the panel already uses exactly this pattern for its too-large fallback (`.reviewDoc` in `CockpitPanels.module.css:87-95`). Spec §5.1's "a single scrolling element" is satisfied — the alignment property comes from the grid rows, not from which element scrolls.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/cockpit/SideBySideDiffView.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { lineDiff, pairRows, refineRuns } from "./diff";
import { SideBySideDiffView } from "./SideBySideDiffView";

function renderCompare(a: string, b: string) {
  return render(
    <SideBySideDiffView
      pairs={pairRows(refineRuns(lineDiff(a, b)))}
      olderLabel="abc1234 · older"
      newerLabel="def5678 · newer"
    />,
  );
}

describe("SideBySideDiffView", () => {
  it("names which side is which", () => {
    renderCompare("alpha", "alpha");
    expect(screen.getByText(/abc1234/)).toBeInTheDocument();
    expect(screen.getByText(/def5678/)).toBeInTheDocument();
  });

  it("puts old text in the left column and new text in the right", () => {
    const { container } = renderCompare("recruit 40 patients", "recruit 120 patients");
    const left = container.querySelector('[data-side="left"][data-diff="del"]');
    const right = container.querySelector('[data-side="right"][data-diff="add"]');
    expect(left?.textContent).toBe("recruit 40 patients");
    expect(right?.textContent).toBe("recruit 120 patients");
  });

  it("highlights only the changed words, not the whole line", () => {
    const { container } = renderCompare("recruit 40 patients", "recruit 120 patients");
    const delSpans = [...container.querySelectorAll('[data-span="del"]')].map((n) => n.textContent);
    const addSpans = [...container.querySelectorAll('[data-span="add"]')].map((n) => n.textContent);
    expect(delSpans).toEqual(["40"]);
    expect(addSpans).toEqual(["120"]);
  });

  it("renders a filler cell, with no add or del tint, opposite an insertion", () => {
    const { container } = renderCompare("alpha\ndelta", "alpha\nbravo\ndelta");
    const fillers = container.querySelectorAll("[data-filler]");
    expect(fillers).toHaveLength(1);
    const filler = fillers[0];
    expect(filler.getAttribute("data-side")).toBe("left");
    expect(filler.textContent).toBe("");
    // The change is on the OTHER side of the row; tinting this cell would say
    // something was deleted here, and nothing was.
    expect(filler.getAttribute("data-diff")).toBeNull();
  });

  it("emits two cells per pair, so a pair is one grid row", () => {
    const { container } = renderCompare("alpha\nbravo", "alpha\nBRAVO");
    const pairs = pairRows(refineRuns(lineDiff("alpha\nbravo", "alpha\nBRAVO")));
    const cells = container.querySelectorAll("[data-side][data-cell]");
    expect(cells).toHaveLength(pairs.length * 2);
  });

  it("renders repeated identical lines without collapsing them", () => {
    const { container } = renderCompare("same\nsame\nsame", "same\nsame\nsame");
    const cells = container.querySelectorAll('[data-side="left"][data-cell]');
    expect(cells).toHaveLength(3);
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && pnpm vitest run src/cockpit/SideBySideDiffView.test.tsx`

Expected: FAIL — cannot resolve `./SideBySideDiffView`.

- [ ] **Step 3: Write the stylesheet**

Create `frontend/src/cockpit/SideBySideDiffView.module.css`:

```css
/* The component owns its scroller so `position: sticky` on the header resolves
   against an element this file controls. Mirrors .reviewDoc in
   CockpitPanels.module.css, which the same panel already uses for its
   too-large fallback. */
.scroll {
  max-height: 60vh;
  overflow: auto;
  border: 1px solid var(--app-border);
  border-radius: 6px;
}

/* Two equal columns. Each DiffPair contributes two cells on ONE grid row, which
   is what keeps the sides aligned — there is no second scrollbar to sync, so
   there is nothing that can drift. */
.grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  font-size: 0.85rem;
}

/* Sticky needs an opaque background or the content slides visibly beneath it —
   the same lesson recorded in EditorToolbar.module.css. --app-editor is the
   token `body` uses (theme/theme.css:35). */
.header {
  position: sticky;
  top: 0;
  z-index: 1;
  background: var(--app-editor);
  border-bottom: 1px solid var(--app-border);
  padding: 0.3rem 0.5rem;
  font-weight: 600;
}

.cell,
.filler {
  white-space: pre-wrap;
  padding: 0.15rem 0.5rem;
  /* A blank filler cell must still occupy its row, or the following rows fall
     out of step with the other column. */
  min-height: 1.25em;
}

.cell[data-diff="del"] { background: rgba(248, 81, 73, 0.15); }
.cell[data-diff="add"] { background: rgba(46, 160, 67, 0.15); }

/* Word spans sit INSIDE an already-tinted cell, so they are stronger than the
   cell tint to read as "this is the part that changed". */
.cell span[data-span="add"] { background: rgba(46, 160, 67, 0.4); }
.cell span[data-span="del"] {
  background: rgba(248, 81, 73, 0.4);
  text-decoration: line-through;
}

/* The gutter between the columns. */
.cell[data-side="left"],
.filler[data-side="left"],
.header[data-side="left"] {
  border-right: 1px solid var(--app-border);
}
```

- [ ] **Step 4: Write the component**

Create `frontend/src/cockpit/SideBySideDiffView.tsx`:

```tsx
import { Fragment } from "react";

import type { DiffPair, DiffRow } from "./diff";
import styles from "./SideBySideDiffView.module.css";

function Cell({ row, side }: { row: DiffRow | null; side: "left" | "right" }) {
  // No counterpart on this side. The cell still occupies the grid row — that
  // blank space is what keeps the following rows in step, and it carries no
  // add/del tint because nothing happened HERE; the change is opposite.
  if (!row) return <div className={styles.filler} data-side={side} data-filler="" />;
  return (
    <div className={styles.cell} data-side={side} data-cell="" data-diff={row.tag}>
      {row.spans
        ? row.spans.map((s, k) => (
            <span key={`${s.tag}:${k}`} data-span={s.tag}>
              {s.text}
            </span>
          ))
        : row.text}
    </div>
  );
}

export function SideBySideDiffView({
  pairs,
  olderLabel,
  newerLabel,
}: {
  pairs: DiffPair[];
  olderLabel: string;
  newerLabel: string;
}) {
  // Content-aware keys, matching DiffView's precedent: pairs are stateless and
  // recomputed each render, so an array index would be a foot-gun if the diff
  // ever reorders, and repeated identical lines are ordinary in prose.
  const seen: Record<string, number> = {};
  return (
    <div className={styles.scroll} data-testid="side-by-side-diff">
      <div className={styles.grid}>
        <div className={styles.header} data-side="left">
          older · {olderLabel}
        </div>
        <div className={styles.header} data-side="right">
          newer · {newerLabel}
        </div>
        {pairs.map((p) => {
          const base = `${p.left?.tag ?? "-"}:${p.left?.text ?? ""} ${p.right?.tag ?? "-"}:${p.right?.text ?? ""}`;
          const n = (seen[base] = (seen[base] ?? 0) + 1);
          return (
            <Fragment key={`${base}#${n}`}>
              <Cell row={p.left} side="left" />
              <Cell row={p.right} side="right" />
            </Fragment>
          );
        })}
      </div>
    </div>
  );
}
```

- [ ] **Step 5: Run to verify it passes**

Run: `cd frontend && pnpm vitest run src/cockpit/SideBySideDiffView.test.tsx`

Expected: **6 tests passed**.

- [ ] **Step 6: Typecheck, build, commit**

```bash
cd frontend && pnpm exec tsc --noEmit && pnpm build
cd /home/parlar_ai/dev/call-monitor
git add frontend/src/cockpit/SideBySideDiffView.tsx frontend/src/cockpit/SideBySideDiffView.module.css frontend/src/cockpit/SideBySideDiffView.test.tsx
git commit -m "feat(cockpit): SideBySideDiffView — two columns, one grid, one scroller

A DiffPair is two cells on ONE grid row, so the sides cannot drift: there is
no second scrollbar to keep in sync. A filler cell still occupies its row --
that blank space is what keeps the following rows in step -- and carries no
add/del tint, because nothing happened on that side.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: `layout` in the History tab's URL state

**Files:**
- Modify: `frontend/src/cockpit/historyNav.ts`
- Modify: `frontend/src/cockpit/historyNav.test.ts`

**Interfaces:**
- Produces: `export type DiffLayout = "split" | "unified"`; `HistoryNav` gains `layout: DiffLayout` and `setLayout: (x: DiffLayout) => void`. Task 5 consumes both.

- [ ] **Step 1: Write the failing tests**

Append to `frontend/src/cockpit/historyNav.test.ts`, inside the existing top-level `describe` (match the file's existing render/harness helper — read the file first and reuse it rather than introducing a second one):

```ts
it("defaults the layout to split when the parameter is absent", () => {
  const nav = renderNav("?tab=history");
  expect(nav().layout).toBe("split");
});

it("reads layout=unified from the URL", () => {
  const nav = renderNav("?tab=history&layout=unified");
  expect(nav().layout).toBe("unified");
});

it("normalises an unrecognised layout to split rather than rejecting it", () => {
  const nav = renderNav("?tab=history&layout=sideways");
  expect(nav().layout).toBe("split");
});

it("writes the layout to the URL", async () => {
  const nav = renderNav("?tab=history");
  await act(async () => nav().setLayout("unified"));
  expect(nav().layout).toBe("unified");
});

it("keeps the layout when the artifact changes", async () => {
  // The layout is how the user reads, not a property of what is selected.
  // Clearing it here would silently undo their choice mid-session.
  const nav = renderNav("?tab=history&layout=unified");
  await act(async () => nav().setArtifact("spec"));
  expect(nav().layout).toBe("unified");
});

it("keeps the layout when the selection changes", async () => {
  const nav = renderNav("?tab=history&layout=unified");
  await act(async () => nav().toggle("abc1234"));
  expect(nav().layout).toBe("unified");
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && pnpm vitest run src/cockpit/historyNav.test.ts`

Expected: FAIL — `layout` and `setLayout` do not exist on `HistoryNav`.

- [ ] **Step 3: Implement**

In `frontend/src/cockpit/historyNav.ts`:

Add after the `artifactParam` function:

```ts
export type DiffLayout = "split" | "unified";

/** Anything unrecognised normalises to `split`, matching artifactParam's
 *  posture: a URL is user input, and a typo should show the default view
 *  rather than an error. */
function layoutParam(sp: URLSearchParams): DiffLayout {
  return sp.get("layout") === "unified" ? "unified" : "split";
}
```

Add to the `HistoryNav` type, after `toggle`:

```ts
  /** How the comparison renders. Not cleared by artifact or selection changes —
   *  it is a display preference, not part of what is selected. */
  layout: DiffLayout;
  setLayout: (x: DiffLayout) => void;
```

Inside `useHistoryNav`, add after `const b = shaParam(sp, "b");`:

```ts
  const layout = layoutParam(sp);
```

Extend the `write` callback's parameter type and body. Replace:

```ts
    (next: { artifact?: Artifact; a?: string | null; b?: string | null }) =>
      setSp((prev) => {
        const out = new URLSearchParams(prev);
        if (next.artifact !== undefined) out.set("artifact", next.artifact);
```

with:

```ts
    (next: { artifact?: Artifact; a?: string | null; b?: string | null; layout?: DiffLayout }) =>
      setSp((prev) => {
        const out = new URLSearchParams(prev);
        if (next.artifact !== undefined) out.set("artifact", next.artifact);
        if (next.layout !== undefined) out.set("layout", next.layout);
```

Add before the `return`:

```ts
  const setLayout = useCallback((x: DiffLayout) => write({ layout: x }), [write]);
```

And change the return to:

```ts
  return { artifact, a, b, layout, setArtifact, toggle, setLayout };
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd frontend && pnpm vitest run src/cockpit/historyNav.test.ts`

Expected: all existing cases plus **6 new**, all passing.

- [ ] **Step 5: Typecheck and commit**

```bash
cd frontend && pnpm exec tsc --noEmit
cd /home/parlar_ai/dev/call-monitor
git add frontend/src/cockpit/historyNav.ts frontend/src/cockpit/historyNav.test.ts
git commit -m "feat(cockpit): layout in the History tab's URL state

?layout=split|unified, defaulting and normalising to split. Deliberately
NOT cleared when the artifact or the selection changes: it is how the user
reads, not a property of what is selected, and clearing it would silently
undo their choice mid-session.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Wire the toggle into `HistoryPanel`

**Files:**
- Modify: `frontend/src/cockpit/HistoryPanel.tsx`
- Modify: `frontend/src/cockpit/HistoryPanel.test.tsx`

**Interfaces:**
- Consumes: `pairRows` (Task 2), `SideBySideDiffView` (Task 3), `nav.layout` / `nav.setLayout` (Task 4).
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Write the failing tests**

Append to `frontend/src/cockpit/HistoryPanel.test.tsx`, reusing the file's existing render harness and mock helpers (read the file first — it already mocks `useVersions`/`useVersion`; do not introduce a second mocking style):

```tsx
it("renders the side-by-side view by default when two versions are selected", async () => {
  renderPanelWithTwoSelected();
  expect(await screen.findByTestId("side-by-side-diff")).toBeInTheDocument();
});

it("renders the unified view when the URL asks for it", async () => {
  renderPanelWithTwoSelected({ search: "&layout=unified" });
  expect(await screen.findByTestId("version-diff")).toBeInTheDocument();
  expect(screen.queryByTestId("side-by-side-diff")).not.toBeInTheDocument();
});

it("offers no layout control until two versions are selected", () => {
  renderPanelWithOneSelected();
  expect(screen.queryByLabelText("Compare layout")).not.toBeInTheDocument();
});

it("switches to unified when the control is used", async () => {
  const user = userEvent.setup();
  renderPanelWithTwoSelected();
  await user.click(screen.getByRole("radio", { name: "Unified" }));
  expect(await screen.findByTestId("version-diff")).toBeInTheDocument();
});

it("labels each column with its short sha", async () => {
  renderPanelWithTwoSelected();
  await screen.findByTestId("side-by-side-diff");
  // Older on the left, newer on the right — the list is newest-first, so the
  // LATER list entry is the older version.
  expect(screen.getByText(/older · 0000002/)).toBeInTheDocument();
  expect(screen.getByText(/newer · 0000001/)).toBeInTheDocument();
});

it("does not offer a layout control for a comparison too large to diff", async () => {
  renderPanelWithTwoSelected({ olderMarkdown: "x\n".repeat(2001), newerMarkdown: "y\n".repeat(2001) });
  expect(await screen.findByText(/too large to compare/)).toBeInTheDocument();
  expect(screen.queryByLabelText("Compare layout")).not.toBeInTheDocument();
});

it("does not describe the too-large fallback as if it were the feature", async () => {
  renderPanelWithTwoSelected({ olderMarkdown: "x\n".repeat(2001), newerMarkdown: "y\n".repeat(2001) });
  const note = await screen.findByText(/too large to compare/);
  // The normal view IS side by side now, so the old copy read as a feature.
  expect(note.textContent).toContain("the full text of each is shown without highlighting");
  expect(note.textContent).not.toContain("shown side by side instead");
});
```

If the existing test file has no `renderPanelWithTwoSelected` / `renderPanelWithOneSelected` helper, add them there, built from the mocking the file already does. Do not duplicate the mock setup inline in each test.

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && pnpm vitest run src/cockpit/HistoryPanel.test.tsx`

Expected: FAIL — no `side-by-side-diff` test id, no layout control.

- [ ] **Step 3: Implement**

In `frontend/src/cockpit/HistoryPanel.tsx`:

Extend the imports:

```tsx
import { lineDiff, pairRows, refineRuns, tooLargeToDiff } from "./diff";
import { SideBySideDiffView } from "./SideBySideDiffView";
```

Add inside `HistoryPanel`, after the `older` / `newer` queries:

```tsx
  // The header timestamps come from the LIST row, not the version payload:
  // VersionContent is {commit_sha, artifact, markdown} and carries no
  // committed_at by design. Degrades to the short sha alone when the row is
  // absent — a state the panel already names elsewhere.
  const labelFor = (sha: string | null) => {
    const short = (sha ?? "").slice(0, 7);
    const row = data?.versions.find((v) => v.commit_sha === sha);
    return row ? `${short} · ${new Date(row.committed_at).toLocaleString()}` : short;
  };
```

Replace the final render branch (the block beginning `{both && ordered && older.data && newer.data && (`) with:

```tsx
      {both &&
        ordered &&
        older.data &&
        newer.data &&
        (tooLargeToDiff(older.data.markdown, newer.data.markdown) ? (
          <Stack gap="xs">
            <Text size="sm" c="dimmed">
              These versions are too large to compare (over 2,000 lines), so the full text of each
              is shown without highlighting.
            </Text>
            {/* Both payloads are already in hand. A note on its own is a dead
                end — the user is told the comparison failed and given nothing
                to read. Older on the left, newer on the right, same order the
                diff would have used. */}
            <Group align="flex-start" gap="sm" grow wrap="nowrap">
              <pre className={styles.reviewDoc} data-testid="version-side">
                {older.data.markdown}
              </pre>
              <pre className={styles.reviewDoc} data-testid="version-side">
                {newer.data.markdown}
              </pre>
            </Group>
          </Stack>
        ) : (
          <Stack gap="xs">
            <Group justify="flex-end">
              <SegmentedControl
                size="xs"
                aria-label="Compare layout"
                value={nav.layout}
                onChange={(v) => nav.setLayout(v as DiffLayout)}
                data={[
                  { label: "Side-by-side", value: "split" },
                  { label: "Unified", value: "unified" },
                ]}
              />
            </Group>
            {nav.layout === "unified" ? (
              <div data-testid="version-diff">
                <DiffView rows={refineRuns(lineDiff(older.data.markdown, newer.data.markdown))} />
              </div>
            ) : (
              <SideBySideDiffView
                pairs={pairRows(refineRuns(lineDiff(older.data.markdown, newer.data.markdown)))}
                olderLabel={labelFor(olderSha)}
                newerLabel={labelFor(newerSha)}
              />
            )}
          </Stack>
        ))}
```

Add `DiffLayout` to the `historyNav` import:

```tsx
import { type DiffLayout, useHistoryNav } from "./historyNav";
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd frontend && pnpm vitest run src/cockpit/HistoryPanel.test.tsx`

Expected: all existing cases plus **7 new**, all passing.

- [ ] **Step 5: Run the whole frontend gate**

```bash
cd frontend && pnpm exec tsc --noEmit && pnpm vitest run && pnpm build
```

Expected: tsc clean; **115 test files** (the measured baseline of 113, plus `diff.pair.test.ts` and `SideBySideDiffView.test.tsx`, with none lost) and **688 tests** — the measured baseline of 658 plus 30 new: 2 in Task 1, 9 in Task 2, 6 in Task 3, 6 in Task 4, 7 in Task 5.

Report the exact printed numbers. A file count that did not reach 115 means a new test file was **not collected** and its tests never ran — vitest reports that as success, so the count is the only signal. A test count below 688 means cases are missing even though nothing is red.

- [ ] **Step 6: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor
git add frontend/src/cockpit/HistoryPanel.tsx frontend/src/cockpit/HistoryPanel.test.tsx
git commit -m "feat(cockpit): side-by-side compare with a toggle back to unified

Split is the default; ?layout=unified renders the existing DiffView. The
control appears only in the compare branch -- it is meaningless with one
version selected, and for a too-large comparison there is no diff to lay out.

Column labels read committed_at from the version LIST row: VersionContent is
{commit_sha, artifact, markdown} and carries no timestamp by design, so a
header field is not a reason to widen the API.

The too-large note said the fallback showed the versions 'side by side
instead' -- which now describes the normal view. It says what it actually
does: the full text of each, without highlighting.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: The browser gate

jsdom performs no layout. Every test above can prove two columns *render*; none can prove they sit side by side, that a pair shares a row, or that the header sticks. This is the gap that hid the selection toolbar sitting 2,529px below a 562px viewport while its whole suite was green.

**Files:**
- Create: `<scratchpad>/gate.mjs` (a scratch file — **not** committed, and **not** placed under `frontend/` or `src/`)

**Interfaces:** none. This task produces evidence, not code.

- [ ] **Step 1: Confirm the app is serving**

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/
```

Expected: `200`. If not, start it:

```bash
cd /home/parlar_ai/dev/call-monitor
tmux new-session -d -s cm-app -c /home/parlar_ai/dev/call-monitor \
  'set -a; source .env; set +a; SPA_MODE=prod uv run --python 3.12 uvicorn call_manager.main:create_app --factory --host 127.0.0.1 --port 8000 > app.log 2>&1'
```

`SPA_MODE=prod` serves the built bundle from `src/call_manager/web/spa/`, so run `cd frontend && pnpm build` and redeploy the bundle first, or the browser will show the pre-change SPA and the gate will measure the wrong thing.

- [ ] **Step 2: Mint a session cookie**

```bash
cd /home/parlar_ai/dev/call-monitor
set -a; source .env; set +a
uv run --python 3.12 python - <<'PY'
import os, psycopg
from call_manager.auth.session_cookie import SessionSigner
url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
WS = "312f51e2-26d2-4ad8-9526-55222e4db60a"  # the only workspace with >2 versions
with psycopg.connect(url) as c:
    uid, epoch = c.execute(
        "select w.owner_id, coalesce(u.session_epoch, 0) from workspaces w "
        "join users u on u.id = w.owner_id where w.id = %s", (WS,)).fetchone()
ck = SessionSigner(secret=os.environ["SESSION_SECRET"]).make(user_id=uid, session_epoch=epoch)
open("/tmp/gate-cookie.txt", "w").write(ck)
print("cookie written for user", uid)
PY
```

Never print the cookie or `SESSION_SECRET` into the transcript.

- [ ] **Step 3: Write the gate script**

Create `gate.mjs` in the session scratchpad:

```js
import { chromium } from '/home/parlar_ai/dev/call-monitor/frontend/node_modules/.pnpm/playwright@1.60.0/node_modules/playwright/index.mjs';
import fs from 'fs';

const WS = '312f51e2-26d2-4ad8-9526-55222e4db60a';
const cookie = fs.readFileSync('/tmp/gate-cookie.txt', 'utf8').trim();
const b = await chromium.launch();
const ctx = await b.newContext({ viewport: { width: 1440, height: 900 } });
await ctx.addCookies([{ name: 'session', value: cookie, domain: '127.0.0.1', path: '/' }]);
const p = await ctx.newPage();
p.on('pageerror', (e) => console.log('  [pageerror]', String(e).slice(0, 300)));

// Pick the two versions furthest apart, so the comparison is long enough to overflow.
const list = await (await ctx.request.get(
  `http://127.0.0.1:8000/api/workspaces/${WS}/history?artifact=draft`)).json();
const shas = list.versions.map((v) => v.commit_sha);
const [newer, older] = [shas[0], shas[shas.length - 1]];

await p.goto(`http://127.0.0.1:8000/cockpit/${WS}?tab=history&artifact=draft&a=${older}&b=${newer}`,
  { waitUntil: 'networkidle' });
await p.waitForSelector('[data-testid="side-by-side-diff"]', { timeout: 20000 });

const r = await p.evaluate(() => {
  const box = document.querySelector('[data-testid="side-by-side-diff"]');
  const lefts = [...box.querySelectorAll('[data-side="left"][data-cell]')];
  const rights = [...box.querySelectorAll('[data-side="right"][data-cell]')];
  const header = box.querySelector('[data-side="left"]');
  const L = lefts[0].getBoundingClientRect();
  const R = rights[0].getBoundingClientRect();

  // 1. adjacent, not stacked
  const adjacent = L.right <= R.left + 1 && Math.abs(L.top - R.top) < 1;

  // 2. a matching pair shares a vertical offset
  const sameRow = lefts.slice(0, 10).every((l, i) =>
    !rights[i] || Math.abs(l.getBoundingClientRect().top - rights[i].getBoundingClientRect().top) < 1);

  // 3. the header genuinely sticks. A container that does not overflow never
  //    moves, so a plain non-sticky header would pass a naive check.
  const overflows = box.scrollHeight > box.clientHeight;
  const headerBefore = header.getBoundingClientRect().top;
  const probe = lefts[1] ?? lefts[0];
  const probeTopBefore = probe.getBoundingClientRect().top;
  box.scrollTop = Math.max(header.getBoundingClientRect().height * 3, 200);
  const headerAfter = header.getBoundingClientRect().top;
  const probeTopAfter = probe.getBoundingClientRect().top;

  return {
    adjacent, sameRow, overflows,
    scrollTop: box.scrollTop,
    scrolledForReal: box.scrollTop > 0 && Math.abs(probeTopBefore - probeTopAfter) > 1,
    headerBefore: Math.round(headerBefore), headerAfter: Math.round(headerAfter),
    headerStuck: Math.abs(headerBefore - headerAfter) < 1,
    cells: lefts.length, viewportH: window.innerHeight,
  };
});
console.log(JSON.stringify(r, null, 1));
await p.screenshot({ path: '/tmp/gate-split.png' });

// 4. the toggle switches the view
await p.getByRole('radio', { name: 'Unified' }).click();
await p.waitForSelector('[data-testid="version-diff"]', { timeout: 10000 });
console.log('toggle -> unified: OK');
await p.screenshot({ path: '/tmp/gate-unified.png' });
await b.close();
```

`scrolledForReal` deliberately checks two things: that `scrollTop` moved **and** that a content row actually shifted. Without the second half, a container that refused to scroll for some unrelated reason would still report a stable header offset and pass.

- [ ] **Step 4: Run the gate and read every field**

Run: `node <scratchpad>/gate.mjs`

Required, all of them:

| Field | Must be | Meaning |
|---|---|---|
| `adjacent` | `true` | columns are horizontally side by side, not stacked |
| `sameRow` | `true` | a pair's two cells share a vertical offset |
| `overflows` | `true` | **fail the gate if false** — the sticky check is vacuous without it |
| `scrolledForReal` | `true` | the container actually moved |
| `headerStuck` | `true` | the header held its viewport offset while content moved |
| `toggle -> unified: OK` | printed | the control switches the rendered view |

If `overflows` is `false`, the comparison chosen was too short. Do not relax the check — pick a longer pair or reduce the viewport height.

- [ ] **Step 5: Look at the screenshots**

Open `/tmp/gate-split.png` and `/tmp/gate-unified.png` and actually look at them. A green measurement with a blank or visibly broken frame is a failed gate. Confirm: two columns, changed words highlighted rather than whole lines, filler space where one side has no counterpart, and the header naming which side is which.

- [ ] **Step 6: Record the evidence**

No commit — this task produces no tracked files. Report in the task report: the JSON from Step 4 verbatim, and what the screenshots showed. Delete `/tmp/gate-cookie.txt` when done.

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §2 scope: History only, three files unchanged | Global Constraints; Tasks 3 and 5 |
| §3 toggle, `layout` URL param, normalisation, not cleared | Task 4; Task 5 Step 3 |
| §4.1 shared candidate pairing, one-directional invariant | Task 1; Task 2 invariant tests |
| §4.2 `blocks()`, existing 16 tests unmodified | Task 1 Steps 3–4 |
| §4.3 `pairRows` rules table | Task 2 Step 1 (one test per row) |
| §5.1 one scroll container, grid rows, filler | Task 3 Steps 3–4 |
| §5.2 exact colours, filler untinted | Task 3 Step 3; test in Step 1 |
| §5.3 sticky header, label from the LIST row, degrade to short sha | Task 3; Task 5 `labelFor` |
| §5.4 no `+`/`-` prefixes | Task 3 Step 4 (the component emits none) |
| §6 states unchanged | Task 5 Step 1 (control absent when <2 selected and when too large) |
| §6.1 too-large copy fix | Task 5 Steps 1 and 3 |
| §7 not built | No task — correct |
| §8.1 unit + property tests | Tasks 1, 2, 3, 4, 5 |
| §8.2 browser gate, all four checks, non-vacuous sticky | Task 6 |

**Placeholder scan:** none. Every code step carries the code; every command carries its expected output.

**Type consistency:** `DiffPair` (Task 2) → consumed by name in Tasks 3 and 5. `DiffLayout` (Task 4) → consumed in Task 5. `SideBySideDiffView`'s props (`pairs`, `olderLabel`, `newerLabel`) match Task 5's call site exactly. `blocks()` stays module-internal and is referenced only inside `diff.ts` (Tasks 1 and 2).

**Known gap, deliberate:** Task 5's tests assume `HistoryPanel.test.tsx` already has a reusable render harness. The implementer must read that file first and reuse its existing mocking rather than adding a second style; the step says so.
