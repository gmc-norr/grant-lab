# Version History — read a version, compare two versions

**Date:** 2026-08-27
**Status:** approved in brainstorming, not yet planned

## 1. Problem

A workspace member cannot see their own history. Versions exist — every save
commits, and `artifact_versions` records who did it — but the sidebar list is
inert text (`cockpit/CockpitSidebar.tsx:138-147`: an `<li>` with two `<span>`s,
no button, no `onClick`). There is no way to open an old version, no way to see
what changed, and no way to go back.

The assistant can already do all three. `tools/history.py` exposes
`get_artifact_history`, `read_artifact_at` and `diff_artifact`, registered at
`tools/registry.py:326-455`. **The AI can read your history and you cannot.**

The only user-facing "restore" in the product is `admin_retention.py:358`
`restore_deleted_workspace` — recovering a whole deleted workspace, not a version.

## 2. Goal

A **History** tab where a member can:

1. see the list of versions, newest first, with who made each one and why;
2. click one and **read** the document as it stood then;
3. select two and see **what changed** between them;
4. **go back** to a version (phase 2 — see §9).

## 3. Non-goals

Explicitly out of scope, each for a measured reason recorded in
`docs/superpowers/specs/2026-08-15-branches-merging-history-graph-design.md`'s
successor analysis (2026-08-27 workflow `wf_bac30c1b-d1c`):

- **Git branches.** Nothing in `src/call_manager/` runs `branch`, `checkout`,
  `switch`, `merge`, `worktree`, `rebase` or `update-ref`. Every content read is
  `read_text()` on one working tree, so a ref move silently re-points all of them.
- **Merging.** Re-hydrating two commits into Yjs docs *concatenates* the
  documents — independent `pycrdt.Doc`s share no item lineage.
- **A branch/merge graph.** Measured 2026-08-27 on the live database:
  `proposed_changes` holds **0 rows**, so the second axis has no data. With one
  line of history the honest rendering is a **list**, and that is what this
  builds. Ribs can be added later without redoing it.
- **Variants** ("a variant is a file, not a ref"). Deliberately deferred: the
  compare view is what a variant would be *read* through, so it must exist first.

## 4. The data, measured

Run against the live database on 2026-08-27, not estimated:

| | |
|---|---|
| live workspaces | 2 (of 11 rows) |
| `artifact_versions` | 22 rows total; largest live workspace **15**, the other **0** |
| `created_by` | `reconciled` 14, `agent` 4, `user` 4 |
| `proposed_changes` | **0** |

Two consequences the design must respect:

- **`created_by` does carry AI-vs-human.** An earlier analysis claimed only
  `'user'` and `'reconciled'` are ever written; the real data has 4 `'agent'`
  rows. The badge is worth showing.
- **The list is small.** 15 rows is the realistic maximum today. No pagination,
  no virtualisation, no lane-assignment layer. Build for tens, not thousands.

## 5. Where it lives

A **fifth top tab** in the cockpit: `Draft | Spec | Scorecard | Panel review |
History`.

`routes/Cockpit.tsx` renders the tab list and switches on `tab`. `?tab` is
validated against **two separate declarations that must be changed together**:

- `type TopTab = "draft" | "spec" | "scorecard" | "review"` — `cockpit/nav.ts:1`
- `export const TOP_TABS: TopTab[] = [...]` — `cockpit/cockpitNav.ts:6`

`tabFromParams` (`cockpitNav.ts:11`) falls back to `"draft"` for anything not in
`TOP_TABS`, so adding the type member without the array leaves `?tab=history`
silently rendering the Draft tab — a passing type-check and a broken URL. The
panel renders inside the existing
`.editorScroll` container (`routes/Cockpit.module.css`), so it scrolls like the
other tabs and needs no scroll handling of its own.

The sidebar Versions group stays as it is. It is a glance; History is the surface.

## 6. Reading a version

### 6.1 Endpoint

```
GET /api/workspaces/{workspace_id}/versions/{commit_sha}?artifact=draft|spec
    -> { "commit_sha": str, "artifact": str, "markdown": str }
```

Modelled exactly on the existing precedent at
`proposal_assist/web/review.py:209-236` — a member-gated `GET` that regex-checks
a sha, calls `read_blob`, and maps `GitCommitError` to 404. Copy its shape:

- a **sync `def`**, not `async` — FastAPI then runs the git subprocess in its
  threadpool instead of blocking the event loop;
- `require_member(workspace_id=..., user=..., session=...)`
  (`proposal_assist/web/_perms.py:29`, keyword-only);
- `workspace_repo_dir(workspaces_dir, workspace_id)`
  (`proposal_assist/paths.py:9`) for the repo;
- `filename_for(Artifact(artifact))` (`ports/artifacts.py:28`) →
  `proposal.md` / `proposal-spec.md`;
- **no commit mutex.** This is a read. `review_file` takes none, and taking one
  would queue reads behind writers for no benefit.

### 6.2 The git call

```python
blob_sha(repo_dir: Path, ref: str, path: str) -> str     # gitio.py:321
read_blob(repo_dir: Path, sha: str) -> str               # gitio.py:343
```

Both are the HTTP-grade pair: they pin `timeout=_GIT_TIMEOUT_SECONDS` (30.0,
`gitio.py:33`) and `GIT_CEILING_DIRECTORIES`, and `read_blob` decodes with
`errors="replace"` so a corrupt blob can never raise into an endpoint.

**`blob_sha` does NOT validate `ref`.** It interpolates `f"{ref}:{path}"`
straight into the argv. That is the exact option-injection surface
`tools/history.py`'s regex exists to close — its module docstring spells out the
attack (`git diff --output=proposal.md` is parsed as an option before the `--`
path separator). **The route MUST validate the sha itself before calling.**

Two copies of that guard already exist — `_SHA_PATTERN` at `history.py:34`
(module-private, returns a message rather than raising) and `_SHA_RE` at
`review.py:36`. Do not add a third. **Extract one shared validator** and have
both existing sites plus the new route use it.

### 6.3 Rendering

Read-only. Render the markdown as text, the way `ReviewPanel.tsx:66` already
renders a review file (`<pre className={styles.reviewDoc}>`). Not the Tiptap
editor — an old version must never look editable.

## 7. Comparing two versions

### 7.1 Where the diff is computed

**In the browser.** `frontend/src/cockpit/diff.ts` already holds a hand-rolled,
dependency-free LCS diff:

```ts
export type DiffRow = { tag: "same" | "add" | "del"; text: string };
export function lineDiff(current: string, proposed: string): DiffRow[];
```

The backend alternative is not available: `diff_artifact_impl`
(`tools/history.py:107`) takes a `ToolContext` — an agent-turn binding carrying
an asyncio write lock and a session factory — and returns a raw unified-diff
string that **nothing in the codebase parses**. Using it from a route would mean
building both a `ToolContext` substitute and a unified-diff parser. Computing in
the browser needs neither, and **no new npm package** — which matters, because
`frontend/pnpm-workspace.yaml` sets a 7-day `minimumReleaseAge` and `pnpm add`
would be refused outright.

So compare fetches **two** version payloads from §6.1 and diffs them client-side.

### 7.2 Line granularity is not good enough — measured

`diff.ts`'s own comment says *"Good enough for short prose sections; richer
word-diff is deferred."* This corpus is exactly the case it defers.

Measured on the live workspace's real `proposal.md`
(`var/workspaces/312f51e2-…/repo/proposal.md`):

| | |
|---|---|
| lines | 37 |
| longest line | **1172 characters** |
| lines over 200 chars | 14 |

It is paragraph-per-line markdown. Under a pure line diff, changing `40 patients`
to `120 patients` renders the **entire 1172-character paragraph** struck through
in red and re-added in green. Every real edit would look like a rewrite.

**Requirement:** the diff refines to **word level inside a changed line**. Rows
tagged `del` immediately followed by rows tagged `add` are paired, and the pair
is re-diffed over words with the same LCS the file already implements, producing
inline spans:

```
We will recruit ~~40~~ **120** patients from the regional biobank, …
```

A `del` with no adjacent `add` (or vice versa) stays a whole-line row. Nothing
about `lineDiff`'s existing signature or its four tests changes; the word pass is
a new exported function beside it.

### 7.3 The renderer must be extracted, not copied

The diff renderer is an inline `<pre>` block **duplicated verbatim** in
`cockpit/ProposalReview.tsx:72-78` and `editor/InlineProposalReview.tsx:73-79`,
styled by three rules at `cockpit/CockpitPanels.module.css:99-105`
(`white-space: pre-wrap`, `[data-diff="add"]` green 15%, `[data-diff="del"]`
red 15%).

A version-compare surface would be the **third copy**. Extract a shared component
first and convert both existing call sites to it. `InlineProposalReview.tsx:39-47`
carries content-aware React keys (tag + text + occurrence) that the other copy
lacks — keep the better one.

### 7.4 The normalisation trap

`markdown_to_pm` is a **many-to-one normaliser**: setext headings become ATX,
`*` bullets become `-`, fence languages are dropped, raw HTML is deleted
outright, and blank lines are inserted. When collab editing was switched on, the
first checkpoint folded a whole-file normalisation into one ordinary-looking
commit.

So a diff spanning that boundary is dominated by changes nobody made.

**Requirement:** when the two selected versions straddle it, the view says so —
a single line above the diff, e.g. *"These versions are from before and after the
editor upgrade, so formatting differences may appear that nobody made."* Do not
silently show the noise, and do not silently hide it.

Detecting the boundary is an open question (§10).

## 8. Empty, loading and failure states

Every one of these is reachable today, not hypothetical:

| State | When | Shows |
|---|---|---|
| No versions | **The user's own live workspace has 0** | "No versions yet. Versions appear when you save one, or when the assistant edits the draft." |
| One version | common early | the list, with compare disabled and a reason |
| Version unreadable | `GitCommitError` → 404 | "That version could not be read" on the row, list intact |
| Sha fails validation | never from the UI; only a hand-typed URL | 422, not 500 |

The zero-version case is the **default experience** for a new workspace and must
be designed, not left to an empty `<ul>`.

## 9. Going back to a version — phase 2

Scoped here, built after §6–§7 ship.

**Forward-only.** Restoring version X writes a **new** version on top whose
content is X's. Nothing is rewritten, no history is reset, and X stays in the
list. Restoring by mistake is undone by restoring again.

**The mechanism is an open question, and the obvious answer is wrong.** Verified:
`workspace_write` (`proposal_assist/web/_writes.py:86-99`) acquires
`acquire_commit_mutex_at_generation` on its own session, and
`collab/kernel.py:138` and `:217` acquire the same exclusive advisory lock. A
kernel call nested inside `workspace_write` therefore takes it twice on two
sessions — a **deterministic self-deadlock**, not a race. Restore must go through
the collab write path *or* the direct write path, never one inside the other.

Restore must also land in the **live room**, not just git: with collab on,
hydration never re-reads git, so a git-only write would be silently clobbered by
the next checkpoint.

The plan for this phase must name the exact write path and prove it under a
concurrent-writer test.

## 10. Open questions

1. **How is the collab-normalisation boundary detected?** Candidates: the
   workspace's `collab_enabled` flip event, the first `collab checkpoint:` commit
   subject, or the earliest `yjs_updates` row. Needs one measurement before §7.4
   can be implemented.
2. **List source: DB or git?** This spec uses the existing
   `GET /workspaces/{id}/versions` (`web/api/workspaces.py:1275`) because it and
   its `useVersions` hook already exist. Known cost: four routine commit paths
   write **no** `artifact_versions` row, and `reconcile` fires only on agent
   launch (`drafting/controller.py:167`), so a collab-only workspace's list can
   lag git. `gitio.list_commits(repo_dir, *, limit) -> tuple[list[CommitInfo], bool]`
   (`gitio.py:462`) is the git-sourced alternative, admin-only today. Ship on the
   DB, state the gap in the UI, and revisit.
3. **Does `VersionOut` need `triggering_turn_id`?** The column exists
   (`db/models.py:1164`) but the response model omits it. It is what would let a
   row say *"because you asked: …"*. Cheap to add; decide before the plan.
4. **Does the spec lock interact with restore?** `ws.spec_locked_at` is
   workspace-scoped and `SpecLockEvent` freezes one `artifact_commit_sha`
   (`web/api/workspaces.py:704-716`). Restoring the spec across a lock boundary
   needs defined behaviour before phase 2.

## 11. Testing

**Backend.** Model on the existing member-gated git-read route tests for
`review_file`. The fixture gap to close first: a repo fixture with **more than
one commit** — compare needs two, and most existing git fixtures build one.

**Frontend.** `routes/Cockpit.test.tsx` stubs `clientMod.api.GET` with
`vi.spyOn`; panel tests mock their api module. The tab addition is testable there.

**The gates that must bite**, each proven by breaking it:

- word-level refinement: a one-word change in a 1172-character paragraph produces
  a row containing **both** unchanged and changed spans, not two whole-line rows;
- sha validation: a `ref` of `--output=proposal.md` is rejected **before** it
  reaches `blob_sha`;
- membership: a non-member gets 403, not a document;
- the zero-version state renders its message rather than an empty list.

Note the suite's false-pass signature: a broken import shows as mass teardown
**ERRORs with zero FAILED**. Judge a mutation on FAILED, never ERROR.
