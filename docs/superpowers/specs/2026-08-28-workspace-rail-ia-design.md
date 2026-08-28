# Workspace rail — information architecture

**Date:** 2026-08-28
**Status:** design, revision 5 — outside review rounds 1-4, 16/16 findings confirmed (one
with a correction).
Not implemented. The two rounds also found **two live data-loss bugs** in shipped code,
fixed separately in `cc50e0e7` and `1f3fd60a` (see §7.3).
**Supersedes nothing.** Follows `2026-08-28-version-graph-side-pane-design.md`, which moved
the version list into the side pane and exposed the problem this spec fixes.

## 1. The problem

The left rail does two unrelated jobs with one strip of icons:

| Icon | Job |
| --- | --- |
| Calls (`search`) | which area of the app |
| Workspaces (`folder`) | which area of the app |
| Current workspace (`edit`) | a view inside the open workspace |

VS Code's rail only ever does the second job — Explorer, Search, Source Control and Debug
are all views of one open folder. It never has to answer "which app am I in", because
there is only one. This app stacked both levels into one strip.

The consequence is that a workspace-level view has nowhere to live, so five of them share
one accordion in the side pane: **Outline, References, Criteria, Versions, Members**
(`CockpitSidebar.tsx:71, 102, 114, 137, 139`). Versions is fourth, collapsed by default,
and in a real workspace it sits below the fold. The user reported exactly this: the
History tab said "Pick two versions from the Versions list in the side pane" and there was
no visible list to pick from.

### 1.1 The deeper incoherence

The app currently runs **half of two different designs**:

- The version **list** lives in the persistent side pane — that is the **VS Code** model,
  where Source Control is a view you keep beside you while you work.
- The version **diff** lives behind a tab that replaces the editor — that is the
  **Overleaf / Google Docs** model, where history is a mode you enter and leave.

Both are coherent designs. Running half of each is what makes the UI feel arbitrary.

## 2. The decision

**Keep the VS Code model. Drop the Overleaf half.**

The deciding argument is specific to this app: **the agent writes versions while the user
works.** "The agent changed Background because you asked X" is worth seeing without
leaving the page. Overleaf and Docs make history a mode because there, versions appear
only when the human saves; here they arrive on their own. The app's visual language is
already VS Code (`2026-06-xx` VS Code reskin phases P1–P3b), so this is also the smaller
leap.

## 3. The organising rule

> **The pane holds workspace context, narrow enough for 276px. The middle holds the
> artifact you are working on.**

Two earlier drafts of this rule were wrong, and both failures are worth keeping:

1. *"Documents in the middle, views on the rail"* — a Scorecard is a view and is far too
   wide for a 276px pane.
2. *"The pane holds lists you pick from"* — checked against the code, and it describes
   only **Outline** and **Versions**. `CockpitSidebar.tsx:104-112` renders References as
   plain `<li>` rows with **no click handler at all**; `:139-187` renders Members as
   information plus add/remove controls, a management tool; and every Criteria button at
   `:123-133` calls `navigate({ top_tab: "scorecard" })`, the **same destination for every
   row**, so it does not pick that criterion.

Rule 2 was a story about the pane, not a description of it. Rule 3 above is what is
actually true of all five, and of VS Code's own pane — Explorer and Source Control are
lists, but Run and Debug is state and controls.

| Surface | Holds | Width |
| --- | --- | --- |
| Rail + pane | Outline, Versions, References, Criteria, Members | 276px, narrow |
| Middle | Draft, Spec, Scorecard, Panel review, a version diff | the rest |

### 3.1 The requirement the rule does carry

A weaker rule still forbids the thing that has cost this project real confusion:

> **A pane row that looks clickable must open something specific to that row.**

**Criteria violates this today** and this spec does not fix it. Every criterion opens the
Scorecard at the top, so the row promises specificity and delivers none — the same species
of defect as the version messages fixed in `c7c3d8e4`. Fixing it needs an anchor per
criterion on the Scorecard (`ScorecardPanel.tsx:92` renders `key={c.index}` with no id)
and a `criterion_index` on `NavTarget` (`nav.ts:3-8` has no such field). That is a
separate slice; it is named here so it is not mistaken for something this design already
handles.

**References is not clickable at all**, which is honest — a row that does nothing and
looks like it does nothing breaks no promise. It stays as it is.

## 4. The rail

Two bands, separated by a rule. The workspace band renders **only** when a workspace is
open (`cockpitIdFromPath(pathname)` returns an id — the existing test in
`ActivityBar.tsx:86`).

```
🔍  Calls              codicon-search        /                  ┐ app band
📁  Workspaces         codicon-folder        /cockpit           ┘
────────────────────────────────────────────────────
📋  Outline            codicon-list-tree     ?view=outline      ┐
🕐  Versions           codicon-history       ?view=versions     │
🔗  References         codicon-references    ?view=references   │ workspace band
☑️  Criteria           codicon-checklist     ?view=criteria     │ (only when open)
👥  Members            codicon-organization  ?view=members      ┘
────────────────────────────────────────────────────
👤  Profiles           codicon-account       /profiles          ┐ bottom, pinned
⚙️  Settings           codicon-gear          /me                │
🛡️  Admin              codicon-shield        /admin             ┘ (admins only)
```

All **ten** codicon names in the diagram were checked against the installed set
(`@vscode/codicons@0.0.45`) and resolve. Four further names were checked as candidates and
rejected: `source-control`, `git-commit`, `link`, `edit`. An earlier revision said
"fourteen", counting the candidates as if they appeared above; they do not.

The dynamic **"Current workspace"** item (`ActivityBar.tsx:102-114`, icon `edit`) is
removed. The workspace band replaces it: its presence is what signals a workspace is open,
and it does so while being useful.

`codicon-history` is chosen over `codicon-source-control` and `codicon-git-commit`: the
feature is called Versions throughout the UI and the DB (`artifact_versions`), and a git
glyph would promise branching this app does not have (see
`2026-08-15-branches-merging-history-graph-design.md` versus the measured 14-agent
verdict: one lane, zero merge commits).

## 5. The pane

The pane shows **exactly one view** — whichever rail icon is active. The accordion is
removed.

`SidebarGroup`'s collapsible chrome is no longer needed for these five, since each is now
alone in the pane. Each view gets a **plain header** — title, and a count where it has
one — so the pane always says what it is showing.

**The header pattern already exists and must be reused.** `CallsFilters:` and
`WorkspacesFilters` head their panes with

```tsx
<Text component="h2" tt="uppercase" fz="0.7rem" fw={600} c="dimmed">Filters</Text>
```

The five workspace views adopt the same element, so every pane in the app is headed
identically. An earlier draft of this spec claimed those two files used `SidebarGroup`;
checked, and they do not — only `CockpitSidebar` and `VersionGraph` do.

**`SidebarGroup` is therefore deleted, not retained.** Once the accordion is gone it has no
consumers left. Deleting it also removes `openWhen`, added in `605f503c` to unfold Versions
on the History tab — a workaround for the accordion, worth nothing once the accordion is
gone, and an escape hatch that would invite a future view to re-create the problem.

## 6. The middle

The top tab strip (`Cockpit.tsx:82-94`) loses **History** and keeps the rest:

**Draft · Spec · Scorecard · Panel review**

A version diff opens in the middle as a fifth destination, `?tab=diff`, reached by picking
versions in the pane rather than by clicking a tab. It is not in the tab strip: you get
there by choosing something, exactly as opening a file in VS Code puts it in the editor
without adding a permanent tab.

Selecting **one** version opens it read-only in the middle; selecting **two** opens the
diff. Both behaviours exist today in `HistoryPanel` and are preserved.

### 6.0 The Draft/Spec switch moves to the pane

`?artifact` decides **whose history the Versions pane lists**. Its control today is a
`SegmentedControl` in `HistoryPanel`'s header (`HistoryPanel.tsx:97-106`) — that is, in the
**middle**. After this design the middle is the comparison and the pane is the list, so the
switch would sit on the thing it does not govern: a user looking at the Versions pane and
wanting Spec history would have to go to the middle to change what the pane shows.

The switch moves into the **Versions pane header**, beside the title, keeping its
`aria-label="Which artifact"` and its two options. `HistoryPanel` loses it; nothing else
about `HistoryPanel` changes, and it continues to read `nav.artifact`.

Changing the artifact still clears both shas (`historyNav.setArtifact`, unchanged): a sha
from the draft's history is not a version of the spec.

### 6.1 Deselecting must not open anything

Clicking an already-selected version **deselects** it (`historyNav.ts:51-52`), and
`selectAndOpen` sets the middle destination unconditionally — so emptying the selection
currently still opens the comparison. `?tab=diff` with no versions selected is a
destination the user did not ask for.

`selectAndOpen` therefore sets `tab=diff` **only when the resulting selection is
non-empty**. A click that empties it leaves `?tab` alone, and the user stays where they
were.

**Order matters, and it is a requirement, not an implementation detail.** The resulting
selection must be computed **before** the dirty-editor guard runs (§7.3). Guarding first
prompts "discard your unsaved edits?" and then discovers that no destination change was
going to happen — a prompt for nothing, which is how users learn to dismiss prompts
without reading them. The test in §11 asserts no prompt on a deselection from a dirty
editor on another tab.

`?tab=diff` with nothing selected remains reachable by hand-editing the URL. It renders
`HistoryPanel`'s existing empty state, which points at the Versions pane.

**That pointer is only true if the pane is showing.** `?view` defaults to `outline`, so a
bare `?tab=diff` would render "pick a version in the Versions pane" beside the **Outline**
— the exact defect this whole design exists to remove, reintroduced by a default. An
earlier revision claimed the pane "is the pane the user is looking at"; it is not.

**`?view` defaults to `versions` when it is absent and the effective tab is the
comparison**, `outline` otherwise. "The comparison" is deliberately not spelled `diff`
here: Slice 1 ships before the rename, and a rule naming only `diff` would leave
`?tab=history` on the Outline with this same message beside it (§10). The set is exactly
the one §7.2's alias defines — `{history}` in Slice 1, `{diff, history}` after.

Same gap-filling shape as the alias, same precedence: an explicit `view` always wins, so
`?tab=diff&view=criteria` shows Criteria.

## 7. URL state

The URL stays the single channel across the AppShell boundary, as it already is
(`cockpitNav.ts`, `historyNav.ts`).

| Param | Picks | Values | Default |
| --- | --- | --- | --- |
| `?view` | rail icon / pane content | `outline` `versions` `references` `criteria` `members` | `versions` when the effective tab is the comparison; `outline` otherwise (§6.1) |
| `?tab` | middle content | `draft` `spec` `scorecard` `review` `diff` | `draft` |
| `?sec` | active section | safe non-negative int | `0` |
| `?artifact` | which artifact's history | `draft` `spec` | `draft` |
| `?a` `?b` | the two versions | sha `[0-9a-f]{7,64}`, lowercased | unset |
| `?layout` | diff rendering | `split` `unified` | `split` |

`?view` parses exactly like `?tab` does today: anything unrecognised normalises to the
default rather than erroring, matching `tabFromParams` and `artifactParam`.

**`?sec` needs a bound it does not have.** `secFromParams` accepts any `/^\d+$/` and
applies `Number` (`cockpitNav.ts:15-17`), so a long digit string yields a value that is not
an integer at all — measured: `sec=` twenty-five nines returns `1e+25`, and
`Number.isSafeInteger` is false. It is then compared against real section indices and
written back to the URL. Add `Number.isSafeInteger` to the predicate, falling back to `0`
like every other invalid value. Small, unrelated to the rail, and in scope only because
this spec pins the contract of the table above.

### 7.0 The rail must MERGE the query, never replace it

The app-band items are plain paths (`/`, `/cockpit`) handed straight to `<Link to>`
(`ActivityBar.tsx:51-56`). The workspace-band items are **query-only** destinations, and a
`<Link to="?view=versions">` resolves to a search string that **replaces** the existing
one — silently dropping `?tab`, `?sec`, `?artifact`, `?a`, `?b` and `?layout`. Clicking
Versions would discard the comparison the user was looking at.

Each workspace-band item's `to` is therefore computed from the current params:

```ts
// Preserve everything, change one key. The rail switches which PANE is showing;
// it must not decide anything about the middle.
function viewHref(sp: URLSearchParams, view: View, pathname: string) {
  const next = new URLSearchParams(sp);
  next.set("view", view);
  return { pathname, search: next.toString() };
}
```

This is required behaviour, not an implementation note: the test in §11 asserts the
resulting URL still carries an unrelated param.

### 7.3 Changing the middle must not discard unsaved edits

Changing `?tab` unmounts the artifact editor and discards its per-section buffers. That is
not a router navigation, so the router blocker cannot see it; `Cockpit.onTab`
(`Cockpit.tsx:38-42`) calls `confirmDiscardIfDirty()` before allowing it.

Round 1 of the outside review found that `selectAndOpen` — the second way to change that
destination, created when the version list moved into the side pane — went around that
guard. Measured: with the editor dirty, clicking a version called `window.confirm` **zero
times** and moved the URL to `tab=history`. Fixed in `cc50e0e7` by moving the guard into
`selectAndOpen` itself, so it covers every caller rather than every call site someone
remembers, and conditional on the destination actually changing.

Round 2 then found that the spec's own words — "any future writer of `?tab` carries the
same obligation" — were pointing at an **existing** writer that did not. `cockpitNav
.navigate` writes `tab` with no guard, and every Criteria row reaches it
(`CockpitSidebar.tsx:127`). Measured the same way: dirty editor, `window.confirm` called
**zero** times, URL moved to `tab=scorecard`. Pre-existing, unrelated to the version-graph
work, fixed in `1f3fd60a`.

**There are three writers of `?tab`, and now three guards:** `Cockpit.onTab`,
`historyNav.selectAndOpen`, `cockpitNav.navigate`. Nothing structural stops a fourth from
being written unguarded — that is how both of these happened. This design does not
consolidate them, and says so rather than implying the problem is solved.

**The guards must survive this design, and one of them changes shape.**

`selectAndOpen`'s guard asks "am I already on the comparison?" by comparing the **raw**
`sp.get("tab")` (`historyNav.ts:148`). Renaming the destination to `diff` while §7.2
aliases `history` → `diff` breaks that comparison: a user arriving on a legacy
`?tab=history` link is on the comparison, but the raw string says `history`, so the guard
would prompt when nothing is being discarded. **The guard must compare the parsed
effective destination, not the raw parameter** — that is, both `history` and `diff` count
as already open.

Required tests: the four existing `selectAndOpen guards unsaved edits` tests pass against
`tab=diff`, **plus** one starting from a legacy `?tab=history` URL that asserts no prompt.
The four `navigate()` guard tests in `cockpitNav.test.tsx` also stay green.

### 7.1 One write, not two

Picking a version must set `a`/`b` **and** `tab=diff` in a single `setSearchParams` call.
Two writes in one handler do not work: react-router 7.18.2 gives a functional updater the
params memoised at **render** time, so both build from the pre-click URL and the second
discards the first. This was measured, and is pinned by a test in `historyNav.test.tsx`
("toggle-then-setTab loses the selection").

`historyNav.selectAndOpen` already does this. Its only change is the value it writes:
`out.set("tab", "history")` at `historyNav.ts:162` becomes `"diff"`.

### 7.2 Old links

`?tab=history` parses as `diff`, so existing bookmarks and any link already shared keep
working. This is a parse-time alias, not a redirect: the URL the user arrived on is not
rewritten, consistent with the decision recorded in `HistoryPanel.tsx` not to rewrite a
URL the user was given.

**Precedence, because the two rules can collide.** The alias supplies `view=versions`
**only when `view` is absent**. An explicit `view` always wins. Without this, a legacy
`?tab=history` URL plus a rail click — which merges, producing
`?tab=history&view=criteria` (§7.0) — would have the alias force the pane back to Versions
and the click would do nothing.

So `?tab=history&view=criteria` shows the **Criteria** pane with the comparison in the
middle. That is a coherent state and the same one `?tab=diff&view=criteria` produces; the
alias only ever fills a gap.

## 8. What does not change

- **Calls** and **Workspaces** already follow the rule — the pane holds their filters
  (`CallsFilters`, `WorkspacesFilters`, `router.tsx:64-65`), the middle holds their list.
  Untouched.
- **Several workspaces at once** is served by **browser tabs**: every workspace is a URL,
  so two workspaces are two tabs, each with its own rail, pane and chat. This is what VS
  Code does with windows and Overleaf with projects. In-app workspace tabs were considered
  and rejected: a workspace is a whole context, not a document, so switching one would have
  to swap the rail, the pane and the chat at once — disorienting, and a real feature to
  build for something browser tabs already do.
- The **right pane** (Chat) and its collapse behaviour.
- The **bottom panel** — Inbox, Problems, Activity, Library, Citations, Claims. Some of
  those are lists and have the same disease, but six more tabs is a separate design
  conversation. Deliberately out of scope.

## 9. What this fixes

Every message argued over on 2026-08-28 stops being needed:

- "Pick two versions from the Versions list in the side pane" — the user is already
  looking at the versions list, because that is the pane they clicked.
- "Only one version so far…", "…is not in the Versions list" and the Clear control remain
  correct and stay, but their audience shrinks: the states they describe are now reached
  from a visible list rather than from a link into a folded group.

## 10. Build order

Two slices, in this order, so the findability fix lands first.

**Slice 1 — rail bands and a single-view pane.**
Add `?view`; split the ActivityBar into two bands with a divider; render the workspace band
only when a workspace is open; make `CockpitSidebar` render one view; delete the dynamic
"Current workspace" item and the now-unused `SidebarGroup`. After this slice Versions is one
click away and nothing is hidden in an accordion. `?tab=history` still works.

**The `?view` default must cover `history` in this slice, not just `diff`.** `diff` does not
exist until Slice 2, so a rule written only for `diff` leaves `?tab=history` defaulting to
`outline` — and `HistoryPanel` still says "pick two versions from the Versions list in the
side pane" while the **Outline** is showing. That is the original complaint, rebuilt at the
slice boundary and living in the app until Slice 2 lands.

So the rule is written once, over the **comparison destination**, whatever it is currently
called: `?view` defaults to `versions` when `?view` is absent and the effective tab is the
comparison. In Slice 1 that set is `{history}`; in Slice 2 it becomes `{diff, history}` via
the alias. Nothing about the rule changes between slices — only the set it consults, which
is the same set §7.2's alias already defines.

**Slice 2 — History tab becomes an opened diff.**
Remove `history` from `TOP_TABS` and `TopTab` (`cockpitNav.ts:6`, `nav.ts:1`) and from the
tab strip (`Cockpit.tsx:86`); add `diff`; point `selectAndOpen` at it; add the
`history`→`diff` parse alias.

## 11. Testing

- **`?view` parsing** — each valid value, plus an unrecognised one normalising to
  `outline`, in the style of the existing `tabFromParams` tests.
- **The rail** — the workspace band is absent with no workspace open and present with one;
  the divider separates the two bands; and for **every** `?view`, **exactly one** workspace
  icon carries `aria-current="page"` and it is the matching one. Asserting only that the
  expected icon is active passes on an implementation that lights all five.
- **The pane** — exactly one view renders at a time. Asserting the chosen view is present
  is not enough: assert the others are **absent**, or the test passes on the accordion this
  slice removes.
- **One URL write** — clicking a version in the pane yields a URL carrying `a` **and**
  `tab=diff`. Assert the resulting URL, not that two functions were called: the latter
  passes while the feature is broken. The existing pinned two-writer test stays.
- **The rail merges** — seed the URL with **every** parameter the merge must preserve:
  `?tab=scorecard&sec=3&artifact=spec&a=<sha>&b=<sha>&layout=unified`. Click a
  workspace-band item and assert all six are unchanged and only `view` differs. Seeding
  with `tab` and `sec` alone is a hollow gate: an implementation that keeps those two and
  drops `artifact`, `a`, `b` and `layout` passes it. Asserting merely that `view=versions`
  is present is weaker still — that passes on the replacing `<Link>` this rule forbids.
- **The comparison default** — a bare `?tab=diff` shows the **Versions** pane, and so does
  a bare `?tab=history`, in Slice 1 as well as after. An explicit `?tab=diff&view=criteria`
  shows Criteria.
- **The artifact switch** — it renders in the Versions pane and no longer in
  `HistoryPanel`; changing it clears both shas.
- **Deselect does not open, and does not prompt** — clicking the only selected version
  leaves `?tab` untouched and clears the sha, with a **dirty editor on another tab** and
  **no `window.confirm` call**. Assert `tab` and the confirm count, not just the absence of
  the sha.
- **The dirty guards survive** — the four `selectAndOpen guards unsaved edits` tests pass
  against `tab=diff`, and the four `navigate()` guard tests stay green. One test added:
  from a legacy `?tab=history` URL with a dirty editor, picking a version prompts **not at
  all**, because the comparison is already open (§7.3). Without it, the raw-string
  comparison would prompt and no existing test would notice.
- **Alias precedence** — `?tab=history&view=criteria` shows the Criteria pane, not
  Versions. A test that only checks `?tab=history` alone passes whichever way precedence
  falls.
- **`?sec` is bounded** — a twenty-five-digit `sec` parses to `0`, not `1e+25`. Assert the
  parsed value, not merely that the app did not crash.
- **Criteria is untouched** — this slice must not make the Criteria rows look more
  specific than they are (§3.1). No new test; a reviewer check.
- **The alias** — `?tab=history` resolves to the diff with the versions pane.
- **Accessibility** — each rail item keeps its `aria-label` and `Tooltip position="right"`
  (`ActivityBar.tsx:76`), and the active item keeps `aria-current="page"`.

Frontend gates unchanged: `pnpm exec tsc --noEmit`, `pnpm vitest run`, `pnpm build`, from
`frontend/`. Baseline at the time of writing: **116 files / 748 tests** (740, plus four from `cc50e0e7` and four from `1f3fd60a`).

## 12. Files

**Modify**
- `frontend/src/shell/nav-items.ts` — split into app, workspace and bottom groups
- `frontend/src/shell/ActivityBar.tsx` — two bands, divider, drop the dynamic item
- `frontend/src/shell/ActivityBar.module.css` — the divider
- `frontend/src/cockpit/cockpitNav.ts` — `?view`; `TOP_TABS` gains `diff`, loses `history`;
  the `history`→`diff` parse alias with its precedence rule (§7.2); `Number.isSafeInteger`
  on `?sec`; keeps `navigate`'s dirty-editor guard
- `frontend/src/cockpit/nav.ts` — `TopTab` union
- `frontend/src/cockpit/CockpitSidebar.tsx` — render one view
- `frontend/src/cockpit/VersionGraph.tsx` — drop `openWhen` and its own `SidebarGroup`;
  head the view with the shared plain header
- `frontend/src/cockpit/historyNav.ts` — `selectAndOpen` writes `tab=diff`, keeps its
  dirty-editor guard (§7.3), and only sets the destination when the selection is non-empty
  (§6.1)
- `frontend/src/routes/Cockpit.tsx` — tab strip and the `diff` destination
- `frontend/src/cockpit/HistoryPanel.tsx` — loses the Draft/Spec `SegmentedControl` to the
  Versions pane (§6.0), and nothing else. It renders the comparison; where it is reached
  from is not its concern

**Delete**
- `frontend/src/cockpit/SidebarGroup.tsx` — no consumers once the accordion is gone. It has
  no test file of its own; its behaviour is covered through `CockpitSidebar.test.tsx` and
  `VersionGraph.test.tsx`, and those assertions go with the accordion.

**Unchanged**
- `frontend/src/cockpit/diff.ts`, `DiffView.tsx`, `SideBySideDiffView.tsx`
- `frontend/src/routes/CallsFilters.tsx`, `WorkspacesFilters.tsx`
- All backend code. This spec touches `frontend/` only.
