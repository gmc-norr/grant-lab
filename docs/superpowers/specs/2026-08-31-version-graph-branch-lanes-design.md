# Version graph with branch lanes — design

**Date:** 2026-08-31
**Status:** approved for planning
**Supersedes nothing.** Extends the merged version-graph side pane (`a2e9308a`,
spec `2026-08-28-version-graph-side-pane`) and absorbs the branch picker shipped
with `880ea243` / `2d8ad49d`.

---

## 1. The problem

The cockpit's Versions view renders two unrelated components stacked:

- `frontend/src/cockpit/panes/BranchPicker.tsx` — **zero Mantine imports, zero
  CSS-module usage** (measured: `grep -cE "@mantine|styles\." → 0`). It is the
  one pane that missed the app-wide Mantine sweep, so it has no `PaneHeader`, no
  dropdown affordance, and no visual relationship to what sits below it.
- `frontend/src/cockpit/VersionGraph.tsx` — properly styled, but draws a single
  hard-coded lane. `VersionGraph.module.css:1-3` justifies that with *"One lane
  — measured across all 14 workspace repos, every one has a single branch"*.
  **That comment is now stale on both counts** and is deleted by this work: the
  survey in §3.1 finds eight repos with a `.git`, and one of them has two
  branches. It is quoted here only to show why the single lane exists, not as a
  current fact.

Two further defects in the picker:

1. Its dropdown lists **every branch twice** — once as a switch item
   (`aria-label={name}`) and once as a delete item (`aria-label={`Delete ${name}`}`)
   — with no visual separation. Switch and destroy sit adjacent and look alike.
2. It renders an **always-visible `<ul>` of branch names** whose only reason to
   exist is `BranchPicker.test.tsx:28`
   (`expect(screen.getByText("v2")).toBeInTheDocument()`). Other tests in the same
   file already open the menu and query `getByRole("menuitem", {name: "v2"})`.
   That stray list is the orphaned `main` text in the reported screenshot.

There is also a capability gap: a branch can only be created at `HEAD`.
`BranchIn` (`web/branches.py`) carries `name` and nothing else, and
`gitio.create_branch` is hard-wired to `git branch <name> HEAD`. A user looking
at a version list cannot branch from the version they are looking at.

## 2. Goal

One pane. The branch is the pane's own subject, the history below it is a real
commit graph with a lane per branch, and both the branch and the commits are
actionable. The look follows **VS Code's Source Control Graph**, using the app's
existing VS Code theme tokens in `frontend/src/theme/theme.css`.

---

## 3. Measured facts this design rests on

All measured on 2026-08-31, not assumed.

### 3.1 The live repositories

Eight workspace repos have a `.git`:

| repo | branches | commits | tips |
|---|---|---|---|
| `2085d2db` | 2 | 4 | `main=6d22146`, `test=402ab4f` |
| `312f51e2` | 1 | 15 | `main=b097b84` |
| the other six | 1 | 1–4 | `main=…` |

`2085d2db` is not divergent: `git rev-list --left-right --count main...test` is
`0  1` — `test` is one commit ahead on the same line. **No repository has a fork
today.** The lane renderer must therefore be correct for the general case and
must degrade to exactly one column, which is what every live workspace will
show.

### 3.2 `--parents` is REQUIRED, not optional

`web/versions.py` scopes history to one artifact file (`proposal.md` /
`proposal-spec.md`). Under a pathspec git applies history simplification, and
**`%P` then reports the real parents, which may not be in the returned set.**

Probed on a synthetic repo where `c2` touches only an unrelated file:

```
git log --branches --topo-order --parents --format='%h|%p|%s' -- proposal.md
  561c26c|26d492c|c3 touches proposal        <- rewritten: previous MATCHING commit

git log --branches --topo-order            --format='%h|%p|%s' -- proposal.md
  561c26c|d2bd64b|c3 touches proposal        <- real parent, NOT in the list
```

Without `--parents` the graph draws an edge to a commit that is not on screen.
`--parents` does **not** add columns to a `--format` output (verified: exactly
3 fields per record before, 3 after; a two-parent merge puts both shas
space-separated inside `%P`).

### 3.3 `--branches` alone loses a detached HEAD

Probed: after `git checkout --detach` and one commit, `git log --branches` omits
that commit entirely; `git log --branches HEAD` includes it. A workspace repo
should never be detached, but a repo that *is* detached must not silently show a
history missing its own tip. **The command takes `--branches HEAD`.**

### 3.4 Merges vanish under a pathspec when TREESAME

In the probe, merge `425321c` was dropped from the pathspec list because its
result matched a parent, leaving two commits sharing one rewritten parent — a
genuine fork the renderer must draw. A merge that *does* change the file is
returned with two parents. Both shapes occur; the algorithm handles N parents.

---

## 4. Scope

**In:**

- Backend: parents on history rows; branch tips on the branches endpoint; an
  optional start point for branch creation.
- Frontend: a pure lane-assignment module, an SVG lane renderer, the VS Code
  row layout, the branch bar with its menu and dialogs.
- Deleting `panes/BranchPicker.tsx` and `panes/BranchPicker.test.tsx`.

**Out — stated so it is a decision, not an oversight:**

- **`HistoryPanel.tsx`'s 13-condition rewrite.** Separate deferred item
  (2026-08-28, agreed with the user). Folding it in doubles the diff and the
  risk. This design does not touch `HistoryPanel.tsx`.
- Merging branches. There is no merge UI today and none is added.
- Remote refs and tags. `--branches` is refs/heads only, deliberately.

---

## 5. Backend

### 5.1 `gitio.py` — `CommitInfo` gains parents

`CommitInfo` becomes:

```python
@dataclass(frozen=True)
class CommitInfo:
    sha: str
    authored_at: datetime
    subject: str
    parents: tuple[str, ...] = ()
```

`list_commits` **always** requests `%P` — a fourth NUL field — so there is one
parser, not a conditional one. Consequences, each of which the plan must carry:

- format becomes `--format=%H%x00%P%x00%aI%x00%s`
- `--parents` is always passed (§3.2; harmless without a pathspec)
- `max_fields = 4 * (limit + 1)`
- the malformed-output guard changes from `% 3` to `% 4`, message text included
- records are 4-tuples

**The parents field must be parsed defensively, not with a bare `.split()`.**
`_read_capped_fields` caps *every* field at `_MAX_FIELD_BYTES`
(`gitio.py:655` — `_MAX_SUBJECT_CHARS * 4` = 2000 bytes). At 41 bytes per
sha-1-plus-space that is **48 parents**; a longer list is cut, and the cut can
land mid-sha. A partial sha would become a lane that waits for a commit that
does not exist — a line running off the bottom of the graph for no reason.

No code path in this application creates a merge at all (there is no merge UI,
and §4 keeps it that way), so this is a defence for imported or damaged repos —
which `list_commits`'s own docstring names as the population it exists to
inspect. The parse:

```python
raw = parents_field.split()
parents = tuple(p for p in raw if len(p) == len(sha) and _HEX_RE.fullmatch(p))
if len(parents) != len(raw):
    logger.warning("dropped %d malformed parent(s) for %s in %s", ...)
```

Same-length-as-`sha` rather than a fixed 40, so a sha-256 repository is handled
without a code change — the same reasoning `ports/shas.py` uses for its 7–64
range. An empty field yields `()`.

The only other caller, `web/api/admin_retention.py`, ignores the new field. Its
behaviour must not otherwise change.

### 5.2 `gitio.py` — a graph flag

```python
def list_commits(
    repo_dir: Path, *, limit: int, pathspec: str | None = None,
    all_branches: bool = False,
) -> tuple[list[CommitInfo], bool]:
```

`all_branches=True` inserts `--branches`, `HEAD` and `--topo-order` into `argv`
before the pathspec separator. Default `False` keeps `admin_retention`'s output
byte-identical.

### 5.3 `gitio.py` — `create_branch` gains a start point

```python
def create_branch(repo_dir: Path, name: str, start: str = "HEAD") -> None:
```

Body becomes `_git(repo_dir, "branch", name, start)`. The docstring's existing
claim about an unborn HEAD stays true for the default.

### 5.4 `gitio.py` — new `branch_tips`

```python
def branch_tips(repo_dir: Path) -> dict[str, str]:
    """Branch name -> the full sha its tip points at.

    Uses `%(refname)`, not `%(refname:short)`: short form is
    ambiguity-dependent and prints `heads/feature` when a tag shares the name
    (the same trap `list_branches` documents).
    """
```

Command: `git for-each-ref --format=%(objectname)%00%(refname) refs/heads`.
NUL between the fields; records separated by newline. A broken ref makes
`for-each-ref` warn on stderr and exit 0, exactly as `git branch` does — mirror
`list_branches`'s `"broken ref" in proc.stderr` check and raise `GitCommitError`.

### 5.5 `proposal_assist/branches.py`

```python
class BranchStartUnknown(BranchError):
    """The requested start commit is not in this repository."""

def create_workspace_branch(
    session: Session,
    *,
    stamp: FenceStamp,
    workspace_id: uuid.UUID,
    name: str,
    workspaces_dir: Path | str,
    from_sha: str | None = None,     # <- the only addition
) -> None:
```

Every existing keyword — `stamp: FenceStamp` included — is preserved verbatim.
`from_sha` is appended last with a default, so no caller changes except the
route.

Ordering requirement — the de-orphan / delete / `flush()` sequence that runs
**before** `git_create_branch` must stay before it. That ordering is
load-bearing and was established by an earlier review; the start point does not
change it.

**The mutex stays, but its justification narrows.** The current docstring says
the commit mutex is taken so that "a checkpoint or section save committing
between the read and the `git branch` would [not] silently found the branch on a
commit the user did not choose." With an explicit `from_sha` that specific race
is gone — the start point is pinned by the caller. The mutex is still required
for the row and orphan handling, and for the `HEAD` default. Update the
docstring to say both things rather than leaving a claim that is now only half
true.

Validation, in this order:

1. `from_sha is None` → behave exactly as today (`start="HEAD"`).
2. `not is_sha(from_sha)` → `BranchNameInvalid` is wrong here; raise
   `BranchStartUnknown`. `is_sha` accepts 7–64 hex (`ports/shas.py`) and exists
   precisely because an unvalidated value reaching git is an option-injection
   vector.
3. The sha must resolve to a commit **in this repo**:
   `git rev-parse --verify --quiet <sha>^{commit}` via a new
   `gitio.commit_exists(repo_dir, sha) -> bool`. False → `BranchStartUnknown`.
   `blob_sha` fails open on an unresolvable ref, so a pre-check is the only
   place a bad value gets a clean answer.

### 5.6 `proposal_assist/web/branches.py`

`BranchIn` keeps its existing `Field` bounds and gains one optional field:

```python
class BranchIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    from_sha: str | None = None
```

`create_branch_route` passes `from_sha=body.from_sha` alongside the `stamp=…`
argument it already builds with `stamp_for_workspace(ws)`.

`_STATUS` gains `BranchStartUnknown: 404`.

`BranchListOut` gains tips — **branch facts live on the branches endpoint, and
only there**, so nothing can disagree with anything. It is the response model of
both `GET /branches` and `POST /branches/switch`, so both gain the field:

```python
class BranchListOut(BaseModel):
    branches: list[str]
    current: str
    tips: dict[str, str]   # branch name -> full sha. NO default: see below.
```

**`tips` takes no default.** With `= {}` the field is satisfiable by silence:
`POST /branches/switch` builds its own response and would return an empty map
that nobody notices, because the switch test only reads `["current"]`. A
required field makes the omission a construction error at the one place that
can still be fixed.

Both responses must come from **one** listing operation.
`list_workspace_branches` gains `tips` in its return value, and both routes
return whatever it produced — a second, hand-built response body for the switch
route is exactly how the two would drift apart.

`BranchRepoNotReady` still answers 409 before any of this is computed.

### 5.7 `proposal_assist/web/versions.py`

`VersionRow` gains `parents: list[str] = []`, populated from `CommitInfo.parents`.
`list_versions` calls `list_commits(..., all_branches=True)`.

Note in the docstring that the list is now **all branches**, not HEAD's history:
the pane draws the graph, and the current branch is shown by which tip carries
the current-branch pill, not by which commits are present.

---

## 6. Frontend — the lane algorithm

New pure module `frontend/src/cockpit/versionLanes.ts`. No React, no I/O — a
function over data, testable across the whole input space. This is deliberate:
the deferred `HistoryPanel` item exists because hand-written conditions over
async inputs were never exhaustive. Lane assignment is exactly the kind of logic
that must not live inside a render.

### 6.1 Types

```ts
export type GraphCommit = { sha: string; parents: string[] };

/** One line crossing a row. `top`/`bottom` are lane indices at the row's top
 *  and bottom edges. `null` means the line terminates at this row's dot. */
export type LaneLink = { top: number | null; bottom: number | null; color: number };

export type LaneRow = {
  sha: string;
  /** the column the dot sits in */
  lane: number;
  links: LaneLink[];
  /** lanes occupied at this row — the caller takes the max for a stable width */
  columns: number;
};

export function assignLanes(commits: GraphCommit[]): LaneRow[];
```

### 6.2 The algorithm

Input is topo-ordered, children before parents (`--topo-order`, §5.2).
State is `lanes: (string | null)[]` — `lanes[i]` is the sha lane `i` is waiting
for.

For each commit `c`:

1. `claimed = every index j where lanes[j] === c.sha`.
   `own = claimed.length ? claimed[0] : firstFreeSlot(lanes)`.
   Leftmost claim wins, which keeps a long-lived lane in its column.
2. Set `lanes[j] = null` for every `j` in `claimed` — every one of those lines
   ends at this dot.
3. For each parent `p` at index `k`: take `own` when `k === 0`, otherwise
   `firstFreeSlot(lanes)`; set `lanes[lane] = p`; record the lane.
   **Do NOT check whether some lane already waits for `p`.** Several lanes may
   hold the same sha at once; that is how two branches stay in separate columns
   until they meet. See the note below — this is the one step where the obvious
   optimisation is wrong.
4. Pop trailing `null`s so the graph narrows again.
5. Emit links:
   - **pass-through** `{top: j, bottom: j}` for every `j` that was occupied
     above by a sha other than `c.sha`;
   - **incoming** `{top: j, bottom: null}` for each `j` in `claimed`;
   - **outgoing** `{top: null, bottom: lane}` for each parent's lane.
   `color = (bottom ?? top) % PALETTE.length`.
6. `columns = max(lanesAbove.length, lanesBelow.length, own + 1)`.

### 6.2.1 Why step 3 must not deduplicate — measured

An earlier revision of this spec said *"if some lane already waits for `p`,
reuse it."* It is wrong, and it is wrong in a way that still produces a picture
where every line joins up. Prototyped both ways over the eight cases in §8 and
5000 randomly generated DAGs. For `A→C`, `B→C`, `C→∅`:

```
  with dedup            without dedup (correct)
    *   A                 *      A
    *   B                 |  *   B
    *   C                 *  /   C
```

With the dedup, `B`'s edge is absorbed into `A`'s lane inside **B's own row**,
so the graph claims `B` merged into `A`. Without it, `B` keeps its column until
`C`'s dot, which is where the two histories actually meet.

**The checker that caught it is the deliverable, not the fix.** A continuity
check — "every line entering a row was left by the row above" — passes the
buggy version, because the lines do join up. The property that fails is
**fidelity**: for every parent edge in the input, some lane must carry it
continuously from the child's row to the parent's row and be consumed at the
parent's dot. `versionLanes.test.ts` must assert fidelity, not continuity.
Measured after the fix: 5000 random DAGs, 0 continuity failures, 0 fidelity
failures, and `claimed.length` reaches **9** — so multi-lane convergence is
routine, and step 1's "leftmost wins" is load-bearing rather than decorative.

Consequences that are correct and must be pinned by tests, not "fixed":

- A **truncated** history leaves lanes waiting for parents that never arrive.
  Those lines run off the bottom of the last row. That is honest — the pane
  already says "Older versions exist than are shown here."
- A lane freed in step 2 and reused in step 3 produces both an incoming and an
  outgoing link on the same column. That is the ordinary case of a commit
  continuing in its own lane, not an exotic one.
- Two adjacent rows may both put a dot in column 1 with no line between them,
  when the first row's lane closed and the column was recycled. Correct, and
  what VS Code does.

### 6.3 Rendering

`frontend/src/cockpit/VersionLane.tsx` — one `<svg>` per row, `aria-hidden`,
height = the row height.

**Width is list-wide, not per-row.** `VersionGraph` computes
`maxColumns = Math.max(...rows.map(r => r.columns))` **once** and passes it to
every `VersionLane`. Sizing each strip by its own `columns` would move the pills
and the subject horizontally from row to row as the graph narrows — the
`LaneRow.columns` field exists to feed that maximum, not to size anything on its
own.

- `LANE_W = 14`, row height `22px` (VS Code's SCM row height). The row height is
  **fixed** so consecutive strips join into continuous lines; the current CSS
  uses `min-height: 1.4em` and a `::before` pseudo-element, both of which go.
- A link with `top === bottom` draws a straight vertical.
- Otherwise it draws an elbow: vertical to mid-height, then a short quadratic to
  the other column, then vertical — the VS Code curve.
- The dot: `r=4`, filled with the palette colour of `lane`, stroked with
  `--app-editor` so it punches out of the lines behind it. Provenance colour
  (`agent` / `user` / `reconciled`) stays, as today's `data-role` does.
- Palette: six colours from the theme file, added as `--app-lane-1..6`.

### 6.4 The row

Valid HTML forbids a button inside a button, so the row is a flex `<li>` of
siblings, not one big button:

```
[ lane svg ] [ ⑂main ⑂test ] [ button: sha  subject ……  2h ] [ ⋯ ]
```

- **Pills** are `<button>`s. Click switches to that branch (via the existing
  confirm dialog). The current branch's pill is filled with `--app-accent`;
  others are outlined in `--app-border-strong`. A branch whose tip commit does
  not touch the selected artifact has no row to sit on and therefore no pill —
  it is still listed in the branch menu.
- The **select button** keeps today's behaviour: `aria-pressed`,
  `nav.selectAndOpen(sha)`, the hover/focus `Popover` card. One URL write per
  click — two `setSearchParams` writers in one handler clobber each other
  (recorded finding, react-router 7.18.2).
- **Relative time** (`2h`, `3d`) is inside the select button, right-aligned,
  `--app-dim`. Absolute time stays in the popover card.
- **`⋯`** is a Mantine `Menu`, revealed on row hover and on focus, but **always
  in the DOM** so keyboard and screen-reader users reach it. `onContextMenu` on
  the row opens the same menu. Items: *Branch from here…*, *Copy commit id*.

### 6.5 The branch bar

`frontend/src/cockpit/BranchBar.tsx`, rendered between `PaneHeader` and the
list. Absorbs everything `BranchPicker` did.

```
VERSIONS 4                    [Draft|Spec]
⑂ test  ▾
```

Menu (Mantine `Menu`), with labels that make the two lists unmistakable — this
is the fix for the switch/delete footgun:

```
SWITCH TO
  ⑂ main
─────────────────
  + Create new branch…
─────────────────
DELETE                (Menu.Label, --app-bad)
  🗑 test
```

Only non-current branches appear in either list. All three dialogs move over
from `BranchPicker` unchanged in wording:

- **switch** — "will disconnect every open editor. Unsaved keystrokes will be
  lost."
- **delete** — "This cannot be undone — the commits on this branch will be
  permanently lost."
- **new branch** — name field. When opened from a row's *Branch from here…* it
  additionally shows the short sha and subject it will branch from, and posts
  `from_sha`. Opened from the bar, it posts no `from_sha` (server default
  `HEAD`), preserving today's behaviour exactly.

### 6.6 States

Unchanged in meaning, restyled: `Could not load version history.` /
`Version history is not available yet.` / `No versions yet.` / `Older versions
exist than are shown here.` The branch bar shows `…` while `useBranches` is
pending, as today.

When `useBranches` errors the bar renders a dimmed, non-interactive current
value rather than disappearing — a 409 `BranchRepoNotReady` is the ordinary
state of a workspace still in setup.

---

## 7. Files

**Create**
- `frontend/src/cockpit/versionLanes.ts`
- `frontend/src/cockpit/versionLanes.test.ts`
- `frontend/src/cockpit/VersionLane.tsx`
- `frontend/src/cockpit/BranchBar.tsx`
- `frontend/src/cockpit/BranchBar.test.tsx`

**Modify**
- `src/call_manager/proposal_assist/citations/gitio.py`
- `src/call_manager/proposal_assist/branches.py`
- `src/call_manager/proposal_assist/web/branches.py`
- `src/call_manager/proposal_assist/web/versions.py`
- `frontend/src/api/branches.ts`, `frontend/src/api/versions.ts`
- `frontend/src/cockpit/VersionGraph.tsx`, `VersionGraph.module.css`,
  `VersionGraph.test.tsx`
- `frontend/src/cockpit/CockpitSidebar.tsx` (drop the `BranchPicker` import)
- `frontend/src/theme/theme.css` (`--app-lane-1..6`)
- `tests/proposal_assist/test_api_branches.py`
- `tests/proposal_assist/web/test_versions_api.py`
- `tests/proposal_assist/test_gitio_branches.py`
- `tests/proposal_assist/test_branch_create_delete.py`
- `docs/ARCHITECTURE.md` — **generated**; regenerate with
  `uv run python scripts/gen_architecture.py` in the same commit that moves code.

**Delete**
- `frontend/src/cockpit/panes/BranchPicker.tsx`
- `frontend/src/cockpit/panes/BranchPicker.test.tsx`

**Regenerate**
- OpenAPI types: `cd frontend && pnpm gen:api`. Known gotcha: run it with
  `EMBEDDING_MODEL=bge-m3` set, or the app fails to construct and the script
  writes a truncated file from stdout.

---

## 8. Testing

**Backend**

- `list_commits` returns parents; a two-parent merge yields both; a root yields
  `()`. Build the repo in the test, do not mock git.
- `all_branches=True` returns a commit that is on a non-checked-out branch and
  that `all_branches=False` omits. This is the test that makes the flag
  observable — a test that only checks the default proves nothing.
- **The `--parents` gate:** a repo whose commits, oldest first, are
  `c1` (touches the artifact), `c2` (touches a different file), `c3` (touches
  the artifact). Assert **`c3`'s parent is `c1`** — the newer matching commit is
  the one whose parent git rewrites; `c1` does not point forward at `c3`.
  Removing `--parents` makes `c3`'s parent come back as `c2`, which is not in
  the returned list, and must redden this test. (§3.2 is the measurement; this
  is the pin.)
- **The detached-HEAD gate:** commit on a detached HEAD, assert it appears.
  Removing `HEAD` from the argv must redden it (§3.3).
- `branch_tips` maps name → full sha for two branches; a name colliding with a
  tag still yields the plain branch name.
- **The switch response carries real tips.** `POST /branches/switch` returns the
  same exact `tips` map as `GET /branches` for the same repo. Without this the
  switch route can return `{}` forever and every test still passes.
- A parents field containing a token that is not a full-length hex sha yields a
  `CommitInfo` without it, and logs. Build the case by feeding
  `_read_capped_fields`' output shape directly rather than trying to make git
  emit 49 parents.
- `POST /branches` with `from_sha` creates the branch at that commit — assert
  via `branch_tips`, not via the response body.
- `from_sha` that is not hex → 404 `BranchStartUnknown`; hex but absent → 404.
- `admin_retention`'s inspect output is unchanged.

**One existing test breaks on purpose.**
`tests/proposal_assist/test_api_branches.py:37` asserts *exact dict equality*:

```python
assert r.json() == {"branches": ["main"], "current": "main"}
```

Adding `tips` reddens it. That is the correct signal, not a nuisance — it is the
only test in the file that would notice a response-shape change at all. Widen it
to include the tips map with the real sha (read from the repo in the test), so
it keeps being an exact-shape assertion. Do **not** loosen it to a subset check:
a subset assertion is a hollow gate, and this file's whole value is that one
line.

**Frontend**

- `versionLanes.test.ts` has **two layers**, and the second is the one that
  matters:

  1. **Eight worked cases**, each asserting the exact `LaneRow[]`: a straight
     line; a fork (`A→C`, `B→C`, `C→∅`); a two-parent merge; three concurrent
     branches; a merge whose second parent is an ancestor of its first; an
     octopus with three parents; a truncated tail whose parent is absent; an
     empty list.
  2. **A fidelity property test.** Generate DAGs with a seeded PRNG (no
     `Math.random` — the failure must be reproducible), 14 commits, ~20% of them
     with two parents, several thousand seeds. Assert for every one:
     - **continuity** — every line entering a row at column `j` was left by the
       row above at column `j`, and vice versa;
     - **fidelity** — for every parent edge whose parent is in the set, some
       lane carries it from the child's row to the parent's row without
       interruption and is consumed at the parent's dot; and every commit's dot
       claims exactly as many lanes as it has children in the set.

     Fidelity is the gate. **Continuity alone passes the deduplicating version
     of step 3 that §6.2.1 rejects** — that is measured, not supposed. A test
     suite with only the continuity check is a hollow gate for this module.
- `VersionGraph.test.tsx`: the current branch's pill is marked current; clicking
  a pill opens the switch confirm and only then mutates; the row `⋯` menu is in
  the DOM before hover; *Branch from here…* posts the row's sha.
- `BranchBar.test.tsx`: switch and delete items are in separately labelled
  groups; the current branch appears in neither.
- The deleted `BranchPicker.test.tsx` cases that describe **behaviour** (confirm
  before switch, confirm before delete, create posts the trimmed name) move into
  `BranchBar.test.tsx`. The case that only asserted the stray always-visible
  list (`getByText("v2")`) is **deleted, not ported** — it existed to justify UI
  that this design removes.

**Whole suite**: `uv run pytest -n 16 --dist loadfile`; expected
`… passed, 1 skipped`. The `1 skipped` is the signal — `0 skipped` means a live
Anthropic API call ran.

---

## 9. Risks

- **`list_commits` is shared.** The 3→4 field change touches every caller's
  parse arithmetic through one function. The `% 4` guard and the
  `admin_retention` regression test are the protection.
- **Nothing in production forks yet** (§3.1), so the fork and merge paths are
  proved only by tests and by synthetic repos. That is acceptable *because*
  `versionLanes` is pure and can be exercised exhaustively; it would not be
  acceptable if the logic lived in the component.
- **Row height becomes load-bearing.** A subject that wraps breaks the lane
  joins. `white-space: nowrap` + ellipsis (already present) must stay.
- **Graph width is unbounded in principle.** The prototype's random DAGs reached
  13 columns from 14 commits, because random graphs have many independent tips.
  Real width is bounded by the number of simultaneously-open branch edges, which
  for `refs/heads` on these repos is the branch count (1 or 2 today, §3.1) plus
  merge fan-out — and this application creates no merges. No cap is specified:
  clamping columns would break fidelity, and the pane already scrolls. If a
  repository ever does produce a wide graph, the lane strip is the thing that
  grows and the subject is the thing that ellipsises, which is the right
  trade — but revisit this before adding any import path that could bring in
  foreign history.
