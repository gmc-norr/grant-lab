# Side-by-Side Version Compare — Design

**Status:** revision 1
**Scope:** the History tab's two-version comparison only.
**Depends on:** `2026-08-27-version-history-compare-design.md` (slice 1, merged `15f14218`).

---

## 1. Goal

Compare two versions of an artifact in two columns — older left, newer right —
with matching text always directly opposite, the way a VS Code diff reads.

Today the comparison is a single stacked column of `+`/`-` rows. It is correct
and it carries word-level highlighting, but reading a rewritten paragraph means
holding the old text in your head while your eye travels down to the new one.

## 2. Scope

**In:** `HistoryPanel`'s compare view.

**Out — deliberately unchanged:** `ProposalReview` (bottom panel) and
`InlineProposalReview` (inside the editor). Both keep the unified `DiffView`.
Two reasons: they render inside narrow panes where two columns would give ~200px
per side, and they are the accept/reject path that actually writes to the draft.
This change touches nothing that mutates a document.

## 3. Layout toggle and URL state

A two-value control — **Side-by-side** (default) and **Unified** — shown only
when two versions are selected. Unified renders today's `DiffView`, unchanged.

The choice lives in the URL as `layout`, beside the History tab's existing
`?tab=history&artifact=…&a=…&b=…`:

- `layout=split` → side-by-side. This is also the default when the parameter is
  absent.
- `layout=unified` → the existing stacked view.
- Any other value → treated as absent, i.e. `split`. Matching `historyNav.ts`'s
  existing posture for `artifact`, an unrecognised value is normalised rather
  than rejected.

`layout` is **not cleared** when the artifact or the selection changes. It is a
display preference about how the user reads, not a property of what is selected;
clearing it would silently undo their choice mid-session.

## 4. The pairing function

### 4.1 Why the pairing must be shared, not re-derived

`refineRuns` already solves this problem for a different purpose. `lineDiff`
prefers deletion on an LCS tie, so a multi-line rewrite emits **every** `del`
before **every** `add`. To decide which words to highlight, `refineRuns` walks
those two runs and pairs them **by index** — `dels[k]` with `adds[k]`.

Side-by-side needs exactly that pairing to decide which lines sit opposite each
other. If the two were computed independently and ever disagreed, the columns
and the colours would describe different edits on the same screen.

So the run-walk is extracted once and both callers use it. Agreement stops being
something a test has to check repeatedly and becomes structural.

### 4.2 The extracted walk

```ts
type Block =
  | { kind: "single"; row: DiffRow }
  | { kind: "change"; dels: DiffRow[]; adds: DiffRow[] };

/** Group a flat diff into single rows and change-blocks, mirroring exactly the
 *  walk refineRuns has always performed:
 *
 *  - A `del` row OPENS a change-block: the contiguous run of `del` rows, then
 *    the contiguous run of `add` rows immediately following it.
 *  - Any other row is a `single`.
 *
 *  Consequences worth stating, because they are easy to get wrong:
 *  `dels` is NEVER empty in a change-block — only a `del` opens one. `adds`
 *  MAY be empty (a pure deletion). A run of `add` rows with no `del` before it
 *  is NOT a change-block; each such row arrives as its own `single`. */
function blocks(rows: DiffRow[]): Block[];
```

`refineRuns` keeps its exact current signature, its current output, and its
current tests. Only its internals change: it maps `blocks()` instead of doing
its own walk. **Every existing test in `diff.test.ts` and `diff.refine.test.ts`
must pass unmodified** — that is the proof the refactor changed nothing.

### 4.3 The new function

```ts
export type DiffPair = { left: DiffRow | null; right: DiffRow | null };

/** Pair a refined diff into side-by-side rows. Takes the OUTPUT of refineRuns,
 *  so `spans` are already present and are carried through untouched. */
export function pairRows(rows: DiffRow[]): DiffPair[];
```

Rules, in block order:

| Block | Emits |
|---|---|
| `single`, tag `same` | one pair, the same row on both sides |
| `single`, tag `add` | `{left: null, right: row}` — an insertion with nothing it replaced |
| `single`, tag `del` | `{left: row, right: null}` — unreachable via `blocks()`, since a `del` always opens a change-block; handled so the function is total rather than relying on that |
| `change`, index `k < min(dels.length, adds.length)` | `{left: dels[k], right: adds[k]}` |
| `change`, leftover `del` (more dels than adds) | `{left: del, right: null}` |
| `change`, leftover `add` (more adds than dels) | `{left: null, right: add}` |

Within a change-block, leftover dels are emitted before leftover adds, so a
shrinking rewrite reads top-to-bottom as "these went away" after the paired
lines. This matches the order `refineRuns` already pushes them in, so the two
layouts present the same edit in the same sequence.

`pairRows([])` returns `[]`.

## 5. Rendering

### 5.1 One scroll container, not two

The two columns are a **CSS grid** inside a **single** scrolling element. Each
`DiffPair` contributes two grid cells on one grid row.

VS Code achieves alignment with two editors, synchronised scrollbars, and
inserted filler lines. A grid reaches the same visible result with no
synchronisation at all: matching text is not scrolled into alignment, it is
already opposite, because it is the same row. There is no second scrollbar, so
there is nothing that can drift.

The accepted cost: the sides cannot be scrolled independently. VS Code's diff
editor does not allow that either, so nothing is lost.

A grid row is as tall as its taller cell. Where one side is `null`, the cell
still occupies the row and renders as filler — this is the blank space VS Code
draws, and it is what keeps the following rows in step.

### 5.2 Cell appearance

Reuses the existing diff palette from `CockpitPanels.module.css` so the two
layouts are recognisably the same feature:

- unchanged: no tint
- `del` cell: `rgba(248, 81, 73, 0.15)`; `del` word spans `rgba(248, 81, 73, 0.4)` with strikethrough
- `add` cell: `rgba(46, 160, 67, 0.15)`; `add` word spans `rgba(46, 160, 67, 0.4)`
- filler (`null`) cell: no tint and no text. It must **not** carry the red or
  green wash — nothing was deleted or added there; the change is on the other
  side of the row.

Text wraps (`white-space: pre-wrap`), so there is no horizontal scrolling and no
per-column horizontal state to keep in sync.

### 5.3 Column headers

A two-cell header row, sticky to the top of the scroll container, naming which
version each column is: the 7-character short sha and the committed timestamp,
with the words **older** and **newer**. Without it, scrolling a long comparison
leaves the reader unsure which side is which.

**The timestamp comes from the version LIST row, not from the version content
payload.** `VersionContent` is `{commit_sha, artifact, markdown}` — it carries
no `committed_at`, by design. `VersionRow` does, `HistoryPanel` already holds
the loaded list, and it already looks both shas up in it to decide direction.
Reuse that lookup. **No backend change is in scope**: adding `committed_at` to
the content endpoint would be a wider change than this feature needs.

If a header's list row is somehow absent, render the short sha alone. That state
is already reachable and already named elsewhere in the panel ("not in this
list"), so the header must degrade rather than block the comparison.

`position: sticky` inside a scrolling ancestor requires an opaque background or
the content slides visibly beneath it — the same lesson recorded in
`EditorToolbar.module.css`. Use `--app-editor`.

### 5.4 Prefixes

The unified view prefixes rows with `+ `, `- `, and two spaces. The side-by-side
view drops them: the column already says which side a line is on, and a literal
`-` in the left gutter of every changed line is noise once position carries the
meaning.

## 6. States and edge cases

All of these already exist in `HistoryPanel` and are **unchanged in behaviour**;
listed here because each must be checked to still behave once a second layout
exists.

| State | Behaviour |
|---|---|
| Fewer than two versions selected | Single-version read view. The layout control is not shown. |
| One selected sha not in the list | "One of the selected versions is not in this list." No layout control. |
| Either version fails to load | "One of those versions could not be read." |
| `history_available: false` | Unchanged — the list names the state. |
| Over `MAX_DIFF_LINES` (2,000) | Fallback: both documents rendered whole, no highlighting. See 6.1. |
| Over `MAX_WORD_MATRIX` (1,000,000) | That pair keeps whole-cell tint and no word spans, in both layouts. |

### 6.1 The too-large fallback message must change

The current copy reads:

> These versions are too large to compare (over 2,000 lines), so they are shown
> side by side instead.

Once the normal view *is* side by side, that sentence describes the fallback as
if it were the feature. Replace with:

> These versions are too large to compare (over 2,000 lines), so the full text
> of each is shown without highlighting.

The fallback's own two-pane rendering is unchanged.

## 7. Not built

- **Collapsing unchanged regions** ("47 unchanged lines ⌄"). Valuable on large
  files; today's largest real document is 37 lines. Deferred, not rejected.
- **Line numbers.** One line in this corpus is a whole paragraph, so "line 7"
  identifies nothing a reader can use. VS Code shows them because code lines are
  short.
- **Editing either side.** History is read-only. Restore remains phase 2 of the
  version-history spec.
- **Character-level diff inside a word.** Word granularity is what shipped and
  what the cap is measured against.

## 8. Testing and gates

### 8.1 What unit tests can prove

- `pairRows`: each rule in §4.3 — unchanged line, paired rewrite, leftover del,
  leftover add, an add-run with no preceding del-run, and `[]`.
- Spans survive: a paired rewrite arrives with `spans` on both sides, carried
  from `refineRuns`.
- The refactor is inert: every existing `diff.test.ts` and `diff.refine.test.ts`
  case passes **unmodified**.
- Component: older text renders in the left column, newer in the right; a
  leftover del leaves the right cell empty; filler cells carry no add/del tint.
- Toggle: `layout=unified` renders the unified `DiffView`; absent or unknown
  `layout` renders side-by-side; switching updates the URL; the control is
  absent when fewer than two versions are selected.

### 8.2 What unit tests CANNOT prove, and what covers it instead

jsdom performs no layout. It can prove two columns *render*; it cannot prove
they sit side by side, that a pair occupies one row, or that anything is
visible. This is precisely the gap that hid the selection-toolbar defect, where
every test was green while the toolbar sat 2,529px below a 562px viewport.

So the merge gate includes a **browser check against the running app**: open a
real comparison in the History tab and confirm, by measurement and by looking at
the screenshot, that

1. the two columns' bounding boxes are horizontally adjacent, not stacked;
2. a matching pair's two cells share the same vertical offset;
3. the header row stays visible after scrolling the container;
4. the toggle switches the rendered view.

A claim about layout that was not measured in a real browser does not count as
verified.

## 9. Files

**Modify**

- `frontend/src/cockpit/diff.ts` — add `blocks()` (internal), `DiffPair`,
  `pairRows()`; `refineRuns` re-expressed over `blocks()` with no signature or
  output change.
- `frontend/src/cockpit/historyNav.ts` — `layout` in `HistoryNav`, a
  `setLayout` writer, normalising unknown values to `split`.
- `frontend/src/cockpit/HistoryPanel.tsx` — the layout control and the branch
  between the two renderers; the §6.1 copy fix.
- `frontend/src/cockpit/CockpitPanels.module.css` — only if a shared token is
  needed; the grid's own rules live with the new component.

**Create**

- `frontend/src/cockpit/SideBySideDiffView.tsx`
- `frontend/src/cockpit/SideBySideDiffView.module.css`

**Tests**

- `frontend/src/cockpit/diff.pair.test.ts` (new)
- `frontend/src/cockpit/SideBySideDiffView.test.tsx` (new)
- `frontend/src/cockpit/HistoryPanel.test.tsx` (extend)
- `frontend/src/cockpit/historyNav.test.ts` (extend)

**Unchanged**

- `frontend/src/cockpit/DiffView.tsx` and its tests
- `frontend/src/cockpit/ProposalReview.tsx`
- `frontend/src/editor/InlineProposalReview.tsx`
