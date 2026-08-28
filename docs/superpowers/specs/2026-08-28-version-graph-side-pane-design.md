# Version Graph in the Side Pane — Design

**Status:** revision 2 (round 1 outside review applied)
**Scope:** slice 1 of two. Slice 2 (real branches / a second lane) is a separate
decision with its own spec — see §10.
**Depends on:** `2026-08-28-side-by-side-diff-design.md` (merged `104221b0`).

---

## 1. Goal

Move the version list out of the History tab and into the cockpit's left side
pane, drawn as a commit graph — dots on a line, newest at top — the way VS Code
puts Source Control in the side bar and the diff in the editor.

Clicking two dots opens the side-by-side comparison in the History tab, which
then has the full editor width to itself.

## 2. Why: the History tab has run out of vertical room

Measured on the running app, 1600×1000 viewport, the 15-version demo workspace:

| | |
|---|---|
| Version list height | **788px** (16 rows) |
| Diff begins at | **y = 848px** |
| Diff's own scroll container | **598px** tall, holding **3036px** |

So a stacked layout makes you scroll 788px of list to reach the diff, and then
scroll again *inside* it — two nested scrollbars, one above the other. At 50
versions the list alone is ~2,500px.

Vertical space cannot be widened. Horizontal space can: the side pane is a
resizable panel and the editor keeps its full width.

### 2.1 What the move buys — and what it does NOT

**It buys vertical space only. It does not widen the diff.** Measured before any
change, 1600×1000 with default panel sizes:

| | |
|---|---|
| Diff container width | **900px** |
| One diff column | **449px** |

The current layout stacks the list *above* the diff, so the diff already spans
the full editor pane. After the move those numbers are **unchanged**; what
disappears is the 788px of list you scroll through to reach it.

This corrects an earlier draft of this section, which implied the move takes
columns "from ~300px to ~450px". The ~300px figure belongs to the **rejected**
alternative — a graph rail *inside* the History tab — which would have taken
width away from the diff. That is the real comparison:

| Layout | Diff column | Scroll before the diff |
|---|---|---|
| Today (stacked) | 449px | 788px |
| Rail inside the tab (rejected) | ~300px | 0 |
| **Side pane (this spec)** | **449px** | **0** |

The side pane wins because it is the only option that costs nothing
horizontally. Do not justify this change on width.

- The side pane is already `<Panel minSize={12} defaultSize={18}>` inside a
  `PanelGroup autoSaveId="cm-shell"` (`shell/AppShell.tsx:94-110`) — it resizes
  and **persists the width**. Default 18% ≈ 276px; it can be dragged wider to
  read subjects and back to reclaim room.

## 3. Scope

**In:** the cockpit side pane's `Versions` group, and `HistoryPanel`.

**Out:** the diff renderer (`SideBySideDiffView`, `DiffView`, `diff.ts`) is
untouched. The `layout` toggle, the direction contract, the too-large fallback
and every state in the merged compare view keep working exactly as they do.

## 4. One list, from one source

There are currently **two** version lists in the UI:

| | Source | Hook |
|---|---|---|
| Side pane `Versions` group | DB — `GET /api/workspaces/{id}/versions` | `api/workspace.ts:21` |
| History tab | git — `GET /api/workspaces/{id}/history` | `api/versions.ts` |

The side pane's group is replaced by the graph, reading the **git-sourced**
`useVersions(id, artifact)` from `api/versions.ts`. That leaves
`api/workspace.ts`'s `useVersions` with **zero callers** (verified: its only
caller is `CockpitSidebar.tsx:44`), so it is deleted.

**An honest note, because it would be easy to oversell this.** The git-sourced
list exists because four routine commit paths write no `artifact_versions` row,
so a DB-sourced list *can* omit versions. On the live demo workspace the two
lists were compared and are **identical** — same 15 shas, same order. This
change is therefore a layout and duplication win, **not** a data-correctness
fix. Do not claim otherwise.

The backend route `GET /api/workspaces/{id}/versions` is **left in place**. It
will have no frontend consumer after this change; removing it touches the
OpenAPI schema and the generated types, which is a separate decision.

## 5. What a row shows at 276px

276px cannot hold a graph glyph, a sha, a role and a full commit subject. In
priority order:

1. **Graph glyph** — a filled dot, joined to the next by a vertical line. One
   lane (§10 explains why there is exactly one today).
2. **Role**, as the existing colour vocabulary: `agent`, `user`, `reconciled`,
   or "not recorded" where the row has no provenance.
3. **Short sha**, 7 characters.
4. **Subject**, truncated to one line with an ellipsis.

The full subject and the exact timestamp are available on hover (`title`), and
in full in the diff's own column header once the version is selected. Dragging
the pane wider reveals more of the subject — that is what the resize handle is
for, and it persists.

Newest at the top, matching the list it replaces and the git-sourced order.

## 6. Clicking a dot

A click must change **two** things: the selection (`a`/`b`) and the active tab
(`tab=history`), so the diff is visible where it opened. Without the tab change
the selection moves silently while the user is looking at the Draft tab.

### 6.1 It must be ONE write. Two writers lose the selection — measured.

The obvious implementation — call `toggle(sha)` then `setTab("history")` — is
**broken**, and not subtly. `useSearchParams`' setter is not a React state
setter. In `react-router` 7.18.2 the functional form is:

```js
nextInit(new URLSearchParams(searchParams))
```

where `searchParams` is memoised from `location.search` **at render time** and
captured in the `useCallback` closure (`react-router/dist/.../useSearchParams`).
It is not a ref that advances between calls. Two calls in one handler therefore
both build from the *pre-click* URL, and the second navigation discards the
first.

Run in this repo's own vitest, with both hooks mounted and one click calling
`toggle("abc1234")` then `setTab("history")`:

```
RESULTING URL >>> tab=history
AssertionError: expected 'tab=history' to contain 'a=abc1234'
```

The selection is gone. **This is a measurement, not a prediction.**

### 6.2 The required mechanism

`useHistoryNav` gains:

```ts
/** Apply toggle's selection rule AND open the History tab in ONE
 *  setSearchParams write. Calling toggle() then setTab() loses the selection —
 *  see §6.1, proven. */
selectAndOpen: (sha: string) => void;
```

It reuses `toggle`'s exact rule (first click fills `a`, second fills `b`, a third
slides the window along, clicking a selected dot clears it) and sets
`tab=history` in the same `setSp` callback. The graph calls **only**
`selectAndOpen`.

`toggle` keeps its current behaviour and its existing tests unchanged — it is
the pure selection rule and `selectAndOpen` is defined in terms of it.

Both parameters already live in the URL (`?tab`, `?artifact`, `?a`, `?b`), and
the side pane is a shell sibling that reads the same `useSearchParams` — the URL
is the established channel across the AppShell boundary
(`cockpit/cockpitNav.ts`, and `CockpitSidebar`'s own header comment). **No new
state mechanism, no props, no context.**

Selected dots are visibly marked in the pane, with `aria-pressed`, matching the
row buttons they replace.

## 7. Draft vs spec

The graph reads `?artifact` — the same parameter the History tab already uses.
The **Draft / Spec toggle stays in the History tab header**, where it is today.

Rejected: moving the toggle into the group header (no room at 276px), and
deriving the artifact from `?tab` (the user would lose the ability to read the
spec's history while looking at the draft).

When `?artifact` is absent the graph shows the draft, matching
`historyNav.ts`'s existing default.

## 8. The History tab after the move

Keeps: the title, the Draft/Spec toggle, the layout toggle
(side-by-side ↔ unified), the compare view, and every state — loading, list
error, `history_available: false`, "not in this list", one-version read, and the
too-large fallback.

Loses: the version list.

Gains: an **empty state**, shown only when **neither** `a` nor `b` is selected —
*"Pick two versions from the Versions list in the side pane to compare them."*
Without it the tab is blank and the control that drives it is somewhere else on
screen, which is the worst version of this layout.

**Not "fewer than two".** One selected version already renders as a read-only
document (`HistoryPanel.tsx:183`, `{!both && older.data && <pre …>}`), and §8
keeps that. Gating the empty state on "fewer than two" would replace a working
one-version read with a placeholder telling the user to pick versions they have
already started picking.

### 8.1 Three messages point at a list that will not be there

These strings survive today only because the list is directly above them. After
the move they name a control on the other side of the screen:

| Line | Today | Must become |
|---|---|---|
| `HistoryPanel.tsx:188` | "That version could not be read. Pick one from the list above." | "…Pick one from the Versions list in the side pane." |
| `HistoryPanel.tsx:198` | "One of those versions could not be read. Pick two from the list above." | "…Pick two from the Versions list in the side pane." |
| `HistoryPanel.tsx:211` | "One of the selected versions is not in this list. Pick two from above." | "One of the selected versions is not in the Versions list. Pick two from the side pane." |

"Keeps every state working" is not the same as "keeps every state's words
correct". All three are in scope.

## 9. States in the side pane

| State | Shown |
|---|---|
| Loading | The group renders its title and count as today; no spinner is added. |
| Request failed | "Could not load version history." |
| `history_available: false` | "Version history is not available yet." |
| Zero versions | "No versions yet." — the existing copy. |
| Truncated (200-commit cap) | "Older versions exist than are shown here." — the History tab's exact copy (`HistoryPanel.tsx:168`); `HISTORY_LIMIT = 200` (`proposal_assist/web/versions.py:42`). |

These mirror the states the History tab already names, so the two surfaces do
not disagree about what is wrong.

## 10. Not built in this slice

**A second lane.** Measured across all 14 workspace repos on this machine:
**one branch each** (always `main`), **zero merge commits**, and **zero rows** in
`proposed_changes`. Every commit has exactly one parent, so the graph is
provably a straight line today. That is not a defect in the graph — it is the
truth about the history, and it is what VS Code draws for a single-branch repo
too.

The lane layer is slice 2, and it is gated on a **product decision the user has
to make**: two specs in this repo contradict each other about whether git
branches should exist at all
(`2026-08-15-branches-merging-history-graph-design.md` says yes; the 14-agent
review recorded in `project_version_control_design_verdict` says no, with four
measured reasons). Slice 1 must not pre-empt that: it draws the spine, and the
spine is identical under either answer.

**Also not built:** commit search or filtering, a date-grouped view, restore
(phase 2 of the version-history spec), and any change to the diff renderer.

## 11. Testing and gates

### 11.1 Unit

- The graph renders one row per version, newest first, with role, short sha and
  a truncated subject.
- `selectAndOpen` leaves a URL containing **both** the new selection and
  `tab=history`, asserted on the RESULTING URL — not as two separate calls. Two
  separate assertions pass while the feature is broken; that is exactly how §6.1
  was found.
- The §6.1 regression itself: a test that calls `toggle` then `setTab` in one
  handler and asserts the selection survives must FAIL. Keep it as a pinned
  demonstration of why `selectAndOpen` exists, or the next person will "simplify"
  it back.
- Clicking a selected dot deselects it.
- Each state in §9 renders its own copy, and the states are distinguishable from
  one another (an empty list is not the same as an unreadable history).
- The History tab shows the §8 empty state only when NEITHER sha is selected,
  and still renders the read-only document when exactly one is.
- The three §8.1 messages name the side pane, and none of them still says "above".
- `CockpitSidebar.test.tsx` references `/versions` on **five** lines: four are
  request stubs (167, 190, 216, 237) that become `/history`; line 178 is
  `findByRole("button", { name: /versions/i })`, which targets the group's
  disclosure button and stays as it is. Counted, not estimated — the group is
  still called "Versions". These edits are required, not optional.

### 11.2 What unit tests cannot prove

jsdom performs no layout, so no unit test can show that the graph fits at 276px,
that the line joins the dots, or that the diff actually gained width. This is
the same blind spot that hid a selection toolbar 2,529px below a 562px viewport,
and that let `1fr 1fr` ship with columns of 13,610px and 43px.

So the merge gate includes a **browser measurement on the running app**:

1. the side pane's `Versions` group renders the graph, and its rows do **not**
   overflow the pane horizontally (`scrollWidth <= clientWidth`);
2. the History tab's diff container is **no narrower than the recorded
   baseline**. Measured before this change, at **1600×1000 with default panel
   sizes**, on the 15-version demo workspace:

   ```
   diffContainerWidth: 900
   leftColumnWidth:    449
   ```

   The gate asserts `>= 900` and `>= 449` under those same fixed conditions. It
   is a **no-regression** check, not a gain: §2.1 records that this change buys
   vertical space, not width. A gate demanding "wider than before" would be
   unsatisfiable and would be quietly relaxed by whoever hit it.
3. clicking two dots in the pane leaves a URL containing **both** `tab=history`
   and both selected shas — the §6.1 failure is invisible to a test that only
   checks the tab changed;
4. the pane can be dragged wider and the rows show more subject.

And a **fail-proof**: the gate must be shown to go red when the thing it checks
is broken. Narrow the pane below the row's minimum and confirm check 1 fails. A
gate that has never gone red is not known to bite.

## 12. Files

**Modify**

- `frontend/src/cockpit/CockpitSidebar.tsx` — the `Versions` group becomes the
  graph; drop the `api/workspace` `useVersions` import.
- `frontend/src/cockpit/CockpitSidebar.module.css` — graph glyph, dot, connector
  line, truncation.
- `frontend/src/cockpit/HistoryPanel.tsx` — remove the version list; add the §8
  empty state (gated on neither selected); reword the three §8.1 messages.
- `frontend/src/cockpit/historyNav.ts` — add `selectAndOpen` (§6.2). `toggle`
  and its tests are unchanged.
- `frontend/src/api/workspace.ts` — delete the now-callerless `useVersions`.
- `frontend/src/cockpit/CockpitSidebar.test.tsx` — `/versions` stubs → `/history`.
- `frontend/src/cockpit/HistoryPanel.test.tsx` — list-rendering tests move out;
  empty-state test added.

**Create**

- `frontend/src/cockpit/VersionGraph.tsx` — the graph rows. Separate from
  `CockpitSidebar.tsx`, which is already 221 lines across five groups (Outline,
  References, Criteria, Versions, Members).
- `frontend/src/cockpit/VersionGraph.module.css`
- `frontend/src/cockpit/VersionGraph.test.tsx`

**Unchanged**

- `frontend/src/cockpit/diff.ts`, `SideBySideDiffView.tsx`, `DiffView.tsx`,
  `historyNav.ts` (its `toggle` is reused as-is), and every diff test.
