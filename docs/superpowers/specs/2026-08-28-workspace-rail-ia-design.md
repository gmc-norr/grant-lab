# Workspace rail — information architecture

**Date:** 2026-08-28
**Status:** design, approved in conversation, not implemented
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

> **The pane holds lists you pick from. The middle holds the thing you picked.**

This is stated deliberately in place of an earlier draft rule ("documents in the middle,
views on the rail"), which was wrong: a Scorecard is a view but is far too wide for a
276px pane. Sorting by *list vs content* puts everything in the right place, Scorecard
included.

Applied:

| Surface | Holds | Because |
| --- | --- | --- |
| Rail + pane | Outline, Versions, References, Criteria, Members | each is a list you pick from |
| Middle | Draft, Spec, Scorecard, Panel review, a version diff | each is content |

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

Every codicon name above was checked against the installed set
(`@vscode/codicons@0.0.45`); all fourteen resolve.

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

## 7. URL state

The URL stays the single channel across the AppShell boundary, as it already is
(`cockpitNav.ts`, `historyNav.ts`).

| Param | Picks | Values | Default |
| --- | --- | --- | --- |
| `?view` | rail icon / pane content | `outline` `versions` `references` `criteria` `members` | `outline` |
| `?tab` | middle content | `draft` `spec` `scorecard` `review` `diff` | `draft` |
| `?sec` | active section | non-negative int | `0` |
| `?artifact` | which artifact's history | `draft` `spec` | `draft` |
| `?a` `?b` | the two versions | sha `[0-9a-f]{7,64}`, lowercased | unset |
| `?layout` | diff rendering | `split` `unified` | `split` |

`?view` parses exactly like `?tab` does today: anything unrecognised normalises to the
default rather than erroring, matching `tabFromParams` and `artifactParam`.

### 7.1 One write, not two

Picking a version must set `a`/`b` **and** `tab=diff` in a single `setSearchParams` call.
Two writes in one handler do not work: react-router 7.18.2 gives a functional updater the
params memoised at **render** time, so both build from the pre-click URL and the second
discards the first. This was measured, and is pinned by a test in `historyNav.test.tsx`
("toggle-then-setTab loses the selection").

`historyNav.selectAndOpen` already does this. Its only change is the value it writes:
`out.set("tab", "history")` at `historyNav.ts:146` becomes `"diff"`.

### 7.2 Old links

`?tab=history` maps to `?tab=diff&view=versions` when parsed, so existing bookmarks and
any link already shared keep working. This is a parse-time alias, not a redirect: the URL
the user arrived on is not rewritten, consistent with the decision recorded in
`HistoryPanel.tsx` not to rewrite a URL the user was given.

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

**Slice 2 — History tab becomes an opened diff.**
Remove `history` from `TOP_TABS` and `TopTab` (`cockpitNav.ts:6`, `nav.ts:1`) and from the
tab strip (`Cockpit.tsx:86`); add `diff`; point `selectAndOpen` at it; add the
`history`→`diff` parse alias.

## 11. Testing

- **`?view` parsing** — each valid value, plus an unrecognised one normalising to
  `outline`, in the style of the existing `tabFromParams` tests.
- **The rail** — the workspace band is absent with no workspace open and present with one;
  each icon marks itself active for its own `?view`; the divider separates the two bands.
- **The pane** — exactly one view renders at a time. Asserting the chosen view is present
  is not enough: assert the others are **absent**, or the test passes on the accordion this
  slice removes.
- **One URL write** — clicking a version in the pane yields a URL carrying `a` **and**
  `tab=diff`. Assert the resulting URL, not that two functions were called: the latter
  passes while the feature is broken. The existing pinned two-writer test stays.
- **The alias** — `?tab=history` resolves to the diff with the versions pane.
- **Accessibility** — each rail item keeps its `aria-label` and `Tooltip position="right"`
  (`ActivityBar.tsx:76`), and the active item keeps `aria-current="page"`.

Frontend gates unchanged: `pnpm exec tsc --noEmit`, `pnpm vitest run`, `pnpm build`, from
`frontend/`. Baseline at the time of writing: **116 files / 740 tests**.

## 12. Files

**Modify**
- `frontend/src/shell/nav-items.ts` — split into app, workspace and bottom groups
- `frontend/src/shell/ActivityBar.tsx` — two bands, divider, drop the dynamic item
- `frontend/src/shell/ActivityBar.module.css` — the divider
- `frontend/src/cockpit/cockpitNav.ts` — `?view`; `TOP_TABS` gains `diff`, loses `history`
- `frontend/src/cockpit/nav.ts` — `TopTab` union
- `frontend/src/cockpit/CockpitSidebar.tsx` — render one view
- `frontend/src/cockpit/VersionGraph.tsx` — drop `openWhen` and its own `SidebarGroup`;
  head the view with the shared plain header
- `frontend/src/cockpit/historyNav.ts` — `selectAndOpen` writes `tab=diff`
- `frontend/src/routes/Cockpit.tsx` — tab strip and the `diff` destination

**Delete**
- `frontend/src/cockpit/SidebarGroup.tsx` — no consumers once the accordion is gone. It has
  no test file of its own; its behaviour is covered through `CockpitSidebar.test.tsx` and
  `VersionGraph.test.tsx`, and those assertions go with the accordion.

**Unchanged**
- `frontend/src/cockpit/HistoryPanel.tsx` — it renders the comparison; where it is reached
  from is not its concern
- `frontend/src/cockpit/diff.ts`, `DiffView.tsx`, `SideBySideDiffView.tsx`
- `frontend/src/routes/CallsFilters.tsx`, `WorkspacesFilters.tsx`
- All backend code. This spec touches `frontend/` only.
