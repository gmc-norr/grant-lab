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

**`blob_sha` does NOT validate `ref`** — it interpolates `f"{ref}:{path}"`
straight into argv. The route MUST validate the sha before calling. But the
reason is **not** the `git diff --output=` file-overwrite that `tools/history.py`'s
docstring describes; that attack does not reach here, and this spec previously
claimed it did. Executed against a throwaway repo:

| passed as `ref` | result |
|---|---|
| `--output=/tmp/pwned` | returned the string back as the "sha" — **no file written** |
| `-h`, `--help` | returned `-h:proposal.md` etc. as the "sha" |
| the same values into `read_blob` | `GitCommitError` — `cat-file -p` puts `-p` before the value |

Two real reasons to validate anyway, both measured above:

1. **`blob_sha` fails open.** A ref it cannot resolve is echoed back *as if it
   were a sha*, silently. That garbage then reaches `read_blob` and dies with a
   confusing `cat-file` message instead of a clean rejection at the boundary.
2. `ArtifactVersion.commit_sha` is plain `Text` with **no shape CHECK**
   (`db/models.py:1163`), so a sha arriving back from the list is not
   pre-validated — it is user input like any other.

Two copies of the guard already exist — `_SHA_PATTERN` at `history.py:35`
(module-private, returns a message rather than raising) and `_SHA_RE` at
`review.py:37`. **They disagree:** history's is `{7,40}` (sha-1), review's is
`{7,64}` (sha-256-ready). Do not add a third. **Extract one shared validator,
use `{7,64}`**, and convert both existing sites.

**Any exception that escapes the handler becomes a 500, not a 4xx.**
`main.py:713-716` registers an app-wide `@app.exception_handler(Exception)`
returning `api_error(500, "internal_error", …)` for every `/api` request. So the
route must catch `GitCommitError` → 404 and reject a bad `artifact` value
itself — `filename_for(Artifact(x))` raises `ValueError` on an unknown string,
which is fail-closed at the filesystem but a **500 on the wire**.

### 6.3 Rendering

Read-only. Render the markdown as text, the way `ReviewPanel.tsx:66` already
renders a review file (`<pre className={styles.reviewDoc}>`). Not the Tiptap
editor — an old version must never look editable.

### 6.4 Draft or Spec — the panel must choose, explicitly

Every read and every diff needs an artifact, and the git history is **shared**:
one repo per workspace holds `proposal.md`, `proposal-spec.md` and five more
files, so a single commit list mixes commits that touched neither.

The History panel carries a **Draft / Spec segmented control**, defaulting to
**Draft**. It drives the `?artifact=` on every read, and it filters the list to
commits that touched that file — `list_commits` is unscoped, so the filter is
the endpoint's job, not the client's. A commit that touched neither artifact
(a bibliography build, a review pass) does not appear under either.

The control's value belongs in the URL beside `?tab=history`, so a link to a
comparison reopens the same one.

### 6.5 What a row says

Goal 1 is *"who made each one and why"*, so both must actually be carried:

- **who** — `created_by` is the role (`user` / `agent` / `system` / `reconciled`);
  measured on live data it really does vary (`reconciled` 14, `agent` 4, `user`
  4), so the badge is meaningful. The *individual* is only in the commit subject
  as free text (`(user:{id})`), in inconsistent formats across eight commit
  sites. Slice 1 shows the **role badge** plus the subject line as written.
- **why** — `ArtifactVersion.triggering_turn_id` exists (`db/models.py:1164`) but
  `VersionOut` omits it. **Add it**, and where present render *"because you
  asked: …"* from the turn's prompt. Where absent (every `reconciled` row, and
  every commit with no DB row at all), the row shows the commit subject alone.
  Absence is normal and must render as ordinary, not as an error.

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

**Direction is part of the contract, not an accident of selection order.**
`lineDiff(current, proposed)` assigns `del` to its **first** argument and `add`
to its second. Compare must therefore always call it **older first**: sort the
two selections by the version list's own order and pass `older, newer`. Both
sides are labelled in the UI with their date and author. Selecting them in the
other order in the list must produce the identical view — that is a test, not a
convention.

**Guard the size.** `lineDiff` allocates a full `(n+1) x (m+1)` number matrix
(`diff.ts:9-14`) — quadratic in the line counts. Both existing callers diff one
section; this is the first whole-document caller. Today's largest document is 37
lines, so the matrix is ~1.4k entries and the concern is theoretical — but the
guard belongs in now, while it is one line: above a line-count ceiling, fall back
to showing the two versions without a diff and say why.

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

**Requirement:** the diff refines to **word level inside a changed line**.

**Pair contiguous RUNS, not adjacent rows.** `lineDiff` prefers deletion on an
LCS tie, so a replacement emits every `del` before every `add`. Measured:

```
lineDiff("alpha\nbravo\nzulu", "ALPHA\nBRAVO\nzulu")
  ->  del:alpha  del:bravo  add:ALPHA  add:BRAVO  same:zulu
```

A rule that pairs "a `del` immediately followed by an `add`" would pair
**`bravo` with `ALPHA`** — the wrong pair — and leave `alpha` and `BRAVO`
unrefined. Instead: take the maximal run of `del` rows and the run of `add` rows
that follows it, and pair them **by index** (`del[i]` with `add[i]`). A leftover
row on either side, where the runs are unequal in length, stays a whole-line row.
The single-line case is the degenerate one (`del,add`) and falls out of the same
rule.

Each pair is re-diffed over words with the same LCS the file already implements,
producing inline spans:

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

**Measured 2026-08-27: the boundary does not exist yet.** All 13 repos under
`var/workspaces/*/repo` were scanned for the `collab checkpoint:` subject that
`checkpoint.py` writes. **Zero of 13 carry one.** No current diff can straddle
it, and `yjs_updates` holds 2 rows — collab editing has barely been used.

That is not a reprieve, it is a deadline: the **first** checkpoint of the
existing draft folds a whole-file normalisation into one ordinary-looking commit,
and the user is editing in the new editor now. So:

- **Slice 1 ships without boundary detection** — there is nothing to detect, and
  building against zero instances means building against a guess.
- The **detector is required before the first `collab checkpoint:` commit exists
  in any live repo**, and the check is one line: does a commit with that subject
  sit between the two selected shas? That is a `git log` the History endpoint is
  already making.
- When it does straddle, the view says so above the diff, e.g. *"These versions
  are from before and after the editor upgrade, so formatting differences may
  appear that nobody made."* Never silently show the noise, never silently hide it.

The plan must re-run the 13-repo scan and record the number it finds, rather than
inheriting this one.

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

**The mechanism is an open question. An earlier revision of this spec stated the
constraint wrongly and the correction is the useful part.**

It claimed that nesting a collab-kernel call inside `workspace_write` is a
deterministic self-deadlock, because both acquire
`acquire_commit_mutex_at_generation`. **That is false.** Both take a
*caller-supplied* `session` (`_writes.py:53`, `kernel.py:138`, `kernel.py:217`),
and Postgres advisory **xact** locks are re-entrant within a transaction — which
`kernel.py:210-212` states outright, in a comment describing this exact case:

> `checkpoint_artifact` re-acquires the same xact lock later — Postgres advisory
> xact locks are re-entrant within a transaction, so that is an instant no-op
> (no deadlock, no second timeout) with one redundant fresh liveness read.

The real rule is narrower, and it is the one the plan must honour: **never open a
second Session while the first holds the mutex.** Re-entering on the *same*
session is a no-op; contending for the same advisory lock from a *different*
session blocks until the first transaction ends. Restore must thread one session
and one transaction through the whole operation.

Restore must also land in the **live room**, not just git: with collab on,
hydration never re-reads git, so a git-only write is silently clobbered by the
next checkpoint.

The plan for this phase must name the exact write path, show the single session
threading through it, and prove it under a concurrent-writer test.

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
2. **Does the spec lock interact with restore?** `ws.spec_locked_at` is
   workspace-scoped and `SpecLockEvent` freezes one `artifact_commit_sha`
   (`web/api/workspaces.py:704-716`). Restoring the spec across a lock boundary
   needs defined behaviour before phase 2.
3. **Is the actor's name shown, or only their role?** §6.4 shows the role badge
   and the user id. Showing a *name* means joining `users` and deciding whether
   every member may see who edited what. Product call, not a technical one.

## 12. Decided during review — was open, now closed

**The list is sourced from git, not from the database.** (Was: "ship on the DB
and state the gap.") The DB list is not merely incomplete — four routine commit
paths write no `artifact_versions` row, and `reconcile` fires only on agent
launch (`drafting/controller.py:167`), so a collab-only workspace's list can lag
git indefinitely. A History tab whose first goal is *"see the list of versions"*
cannot ship knowing it omits versions, and a UI caption does not make a missing
version reachable.

So: `gitio.list_commits(repo_dir, *, limit) -> tuple[list[CommitInfo], bool]`
(`gitio.py:462`, returning `(commits, truncated)`) is the spine, and the
`artifact_versions` rows are an **overlay joined on sha** — every provenance
field optional, rendered "not recorded" when absent. This is the polarity the
2026-08-27 design workflow recommended and it is right for the same reason here.

The rejected alternative was calling `reconcile_workspace_git_drift` before
listing. It is idempotent (`on_conflict_do_nothing`, `reconcile.py:137`) and
unscoped, so it would work — but it is a **write inside a GET**, and it is fenced
work. A read-only slice should not take a lease.

`list_commits` is only *called* from admin code today; it is a plain function in
`gitio`, not gated, so reusing it needs no change to it.

## 11. Testing

**Backend.** Model on the existing member-gated git-read route tests for
`review_file`. The fixture gap to close first: a repo fixture with **more than
one commit** — compare needs two, and most existing git fixtures build one.

**Frontend.** `routes/Cockpit.test.tsx` stubs `clientMod.api.GET` with
`vi.spyOn`; panel tests mock their api module. The tab addition is testable there.

**The gates that must bite**, each proven by breaking it:

- word-level refinement: a one-word change in a 1172-character paragraph produces
  a row containing **both** unchanged and changed spans, not two whole-line rows;
- **run pairing**: a two-line replacement (`del,del,add,add`) refines `del[0]`
  against `add[0]` and `del[1]` against `add[1]` — a test that only covers a
  one-line change cannot fail on the wrong-pair bug and does not count;
- **direction**: selecting the two versions in the opposite order produces an
  identical view, not an inverted one;
- sha validation: a `ref` of `--output=proposal.md` is rejected **before** it
  reaches `blob_sha`, and the response is 4xx — **not** the app-wide 500 from
  `main.py:713-716`;
- an unknown `?artifact=` value is 4xx, not the 500 that an escaping
  `ValueError` from `filename_for` would produce;
- membership: a non-member gets 403, not a document;
- **the git-sourced list contains a commit that has no `artifact_versions` row**
  — the whole reason for §12 — and that row renders with its provenance fields
  absent rather than erroring;
- the zero-version state renders its message rather than an empty list.

Note the suite's false-pass signature: a broken import shows as mass teardown
**ERRORs with zero FAILED**. Judge a mutation on FAILED, never ERROR.
