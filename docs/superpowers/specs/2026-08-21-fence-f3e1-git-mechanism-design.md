# Fence F3e-1 — the mechanism-2 gate for git effects

**Status:** design, **revision 2** (2026-08-21). Successor to F3d3b (`eb7d4a34`).
**Governing architecture:** `2026-08-17-lifecycle-fence-design.md` (why a fence exists).
**Governing mechanism spec:** `2026-08-18-fence-session-ownership-design.md` (how an
effect is expressed). This document is a **sub-slice design**: it does not supersede
either, and where it departs from the F3e section of the mechanism spec it says so and
why (§9).

> **Revision 2 — four confirmed defects, all verified against the tree.** Revision 1
> claimed eight git sites; there are **nine** — phase 0 commits through
> `_run_git(["commit", …])`, which a `write_and_commit_files`-only detector cannot see
> (§3.2). Revision 1 also argued that measuring only the *direct* git caller was "the
> load-bearing choice" that kept the slice bounded. **It was bounded and hollow**: five
> kernel operations and two spec routes take the mutex and then mutate the room before
> reaching `checkpoint_artifact`, so converting the helper alone turns the inventory
> green while an outer operation can still pass delete-then-restore and mutate (§4.2).
> That scoping contradicted revision 1's own §4 rule, "before its first effect of any
> kind". Revision 1's generation plumbing stopped at `checkpoint_artifact`'s ten call
> sites and never traced the route → orchestrator → kernel chain, two links of which
> carry only a `workspace_id` (§7). And revision 1 asserted that
> `WorkspaceGenerationStale` "maps to each caller's existing deleted-workspace outcome"
> — **nothing maps it today** (§8).
>
> **The slice is split as a result.** Removing the direct-caller scoping removed what
> was keeping revision 1 small. See §2.

---

## 1. The hazard

`acquire_commit_mutex_live` (`drafting/locking.py:257`) takes the exclusive commit
mutex and then re-reads `soft_deleted_at` under it. That defeats *delete*.

It does not defeat **delete-then-restore**. A restore clears `soft_deleted_at` and
leaves `lifecycle_generation` at G+2. An actor that captured G before blocking on the
mutex passes the `_live` check and commits to git against a workspace it no longer
knows anything about. The row-level compensation that masks this for `UPDATE`
statements (`2026-08-18` §3.5) does not exist for a git commit at all: git has no target
row for PostgreSQL to re-check, and **a database transaction cannot roll back git**.

`acquire_commit_mutex_at_generation` (`fencing.py:115`, Primitive B) closes exactly
this: mutex, then one scalar `SELECT` of `(soft_deleted_at, lifecycle_generation)`,
raising `WorkspaceDeleted` or `WorkspaceGenerationStale`.

**It was built in F1 and has zero production callers.** Every git-committing site in
the repository today calls `acquire_commit_mutex_live`.

## 2. Scope — F3e-1 is two sub-slices

`2026-08-18` §7 allocated to F3e both the ingest pipeline and the mechanism-2 effects.
That is two subsystems sharing one new mechanism, and one of them (ingest) carries a
design question the spec explicitly defers. F3e-1 takes the mechanism; **F3e-2** takes
ingest, the filesystem effects, the half-ingest stale-generation question, and the
`references.py` reads.

F3e-1 is itself split, for the reason revision 2 exposed: once the enclosing
acquisitions are in scope (§4.2), the conversion reaches eleven functions across four
orchestrators and their routes. That is too much to land with a brand-new detector in
one plan.

**`F3e1a` — the detector and the inventory. Converts nothing.**
Canonical git-sink detection, the sink-completeness guard, the `git_commit` kind, the
`fenced` **measurement** for that kind, the `UNCLASSIFIED` default, and the AST gate
armed with all nine sites pinned as unconverted. Every `git_commit` row lands
`fenced: false`. No production behaviour changes at all.

**`F3e1b` — the conversions.**
The enclosing-acquisition enumeration, the generation plumbing from route to sink, the
stale-outcome matrix, and the per-site runtime proofs. Sites leave the pin list as they
convert.

This is the shape that worked for F3d: `F3d1` built the spine and converted nothing,
`F3d2`/`F3d3` converted callers. The spine slice is where detection gets right, and
F3d1's own execution found 26 defects in its plan — a detector landing alongside eleven
conversions would hide the same class of defect behind a large diff.

**This document specifies both.** The implementation plan is written per sub-slice.

## 3. Detection

### 3.1 Repo-wide, following existing precedent

Every previous widening (F2 → F3, and again in F3d3a) kept detection inside the
**effect scope** — `tools/` plus the declared `RELOCATED_EFFECT_PATHS`. That scope is
tool-centric, and git effects live in services. Measured against the current tree, a
scope-limited git detector discovers **exactly one** site
(`citations/core.py:_insert_citation_locked`) — and that one is the *retiring* flag-off
path (§6). It would miss every site that matters.

Repo-wide discovery is **already how two of the three existing kinds work.** Only
`KIND_TOOL_COMMIT` is guarded by `_in_effect_scope` (`detectors.py:159, 168, 202`).
`KIND_CLAIM_FN` (`:147`) is not, and neither is the `tool_context` scan. The inventory
holds 6 `claim_fn` rows and 5 `tool_context` rows in `analysis/`, `coherence/`,
`phases/`, `review/`, `uncited_claims/` and `drafting/` — all outside `tools/`.

A repo-wide `git_commit` kind is therefore the same shape as `claim_fn`, not an
invention.

### 3.2 What a git sink IS — and the ninth site revision 1 missed

Revision 1 defined a site as a direct call to `write_and_commit_files`. **That is not
what a git commit is in this repository.** There are exactly two mechanisms, confirmed
by sweeping every `"commit"` string argument in `src/`:

| Sink | Location |
|---|---|
| `write_and_commit_files` | runs `["git", "commit", "-q", "-m", message]` at `citations/gitio.py:195` |
| `_run_git(["commit", …])` | `phases/setup.py:407`, inside `Phase0Worker._init_repo` |

Phase 0's `_init_repo` initialises the repo and commits `phase0: initialize empty
artifacts`. It is a real production git effect, its mutex is taken by `_m1`
(`setup.py:350`), and a `write_and_commit_files`-only detector is blind to it. That
makes revision 1's success criterion "all git sites are discovered" false, and — worse —
falsifies its claim that any new git-committing function stops the build until a human
allocates it. A function committing via `_run_git` would simply never appear.

**`SLICE_STATUS` already allocates Phase 0 to `F5`** (`claim_next_setup_job` is an `F5`
row), so the ninth site is `F5`'s. It is discovered here and converted there.

**The detector therefore keys on a DECLARED SINK LIST, plus a guard that the list is
complete.** Following `RELOCATED_EFFECT_PATHS`, the sinks are declared rather than
sniffed:

```
GIT_SINKS = {
    ("citations/gitio.py", "write_and_commit_files"),
    ("phases/setup.py",    "_run_git"),   # only when argv[0] == "commit"
}
```

A **sink-completeness guard** asserts that no `subprocess` invocation anywhere in `src/`
passes an argv beginning `git commit` / `commit` outside a declared sink. A third
mechanism reddens the build rather than silently escaping the gate — which is the
failure this finding *was*.

**Aliases are resolved, not assumed away.** Revision 1's claim that every git-committing
function is discovered was false for `from … import write_and_commit_files as
commit_files`. No alias exists in the tree today, so this is robustness rather than a
current miss, but the claim was overstated. The detector resolves `import ... as` and
`from ... import ... as` bindings per module before matching call targets, and an AST
fixture proves an aliased import is still discovered.

### 3.3 The kind

```
KIND_GIT_COMMIT = "git_commit"
```

A site is any function that calls a declared sink. Discovery is repo-wide over `src/`,
and the row carries the existing shape `{kind, path, symbol, category, fenced, slice}`.

### 3.4 The nine sites and their allocation

Measured against the tree at `eb7d4a34`. Slice names are `SLICE_STATUS` keys (§3.6).

| Path | Symbol | Sink | Slice |
|---|---|---|---|
| `collab/checkpoint.py` | `checkpoint_artifact` | `write_and_commit_files` | **`F3e1b`** |
| `citations/core.py` | `_insert_citation_locked` | `write_and_commit_files` | **`F3e1b`** |
| `citations/human_cite.py` | `insert_citation_for_user` | `write_and_commit_files` | **`F3e1b`** |
| `editing/save.py` | `save_section` | `write_and_commit_files` | **`F3e1b`** |
| `analysis/insert.py` | `insert_from_annotation` | `write_and_commit_files` | `F3e2` |
| `citations/bibliography.py` | `build_bibliography` | `write_and_commit_files` | `F3e2` |
| `web/api/workspaces.py` | `post_init_spec._git` | `write_and_commit_files` | `F3e2` |
| `review/pass_runner.py` | `run_review_pass` | `write_and_commit_files` | **`F4`** |
| `phases/setup.py` | `Phase0Worker._init_repo` | `_run_git(["commit", …])` | **`F5`** |

**Why `save_section` is in.** It is not a citation path. It is in because leaving it out
would give the gate an unexplained hole: it is a sink caller with the same shape as the
others (`acquire_commit_mutex_live` at `editing/save.py:183`, sink at `:275`), and a rule
that skips it needs a reason that does not exist.

### 3.5 The row is not the whole rule — see §4.2

An inventory row is anchored to the function that calls the sink. **That row is not the
unit the fence protects**, and revision 1's central scoping error was treating it as
though it were. The rule in §4 binds every enclosing mutex acquisition on the path to a
sink, whether or not it owns a row. §4.2 enumerates them and specifies the gate that
proves none is missed.

### 3.6 The `SLICE_STATUS` split and the exact inventory arithmetic

**The gate is bidirectional** (`test_each_slice_matches_its_declared_status`): a
`pending` slice needs **every** row `false`, a `complete` slice needs **every** row
`true`. There is no partial state, which is why F3d needed one key per sub-slice
(`inventory.py`'s own comment: "a multi-slice conversion needs one key per slice or the
first flipped row goes red").

Keys use no hyphen — `F3d1`, `F3d2`, `F3d3` — so the new keys are **`F3e1a`**,
**`F3e1b`** and **`F3e2`**, and the existing `"F3e"` key is removed.

`F3e1a` converts nothing and owns **no row of its own**, exactly as `F3b`, `F3c` and
`F3d1` do (their `SLICE_STATUS` comments say so). Its four conversion targets sit in
`F3e1b`, `pending`, until that slice lands.

The two rows currently allocated to `F3e` — `_write_citekey_cache` and
`fetch_reference_impl._do_fetch_and_ingest` — **both belong to `F3e2`**. `merge()`
preserves `prev["slice"]` (`inventory.py:295-298`) because `slice` is a **human-owned**
field, so regeneration will not move them: the reassignment is a hand-edit of
`fence_inventory.json`, proven by regenerating into a temporary copy and comparing it to
the committed file. **Not by `git diff --exit-code`** — on a file the task deliberately
edited, that can never pass. F3d3b shipped that mistake and had it caught twice.

Shape after each sub-slice:

| Slice | After `F3e1a` | After `F3e1b` |
|---|---|---|
| `F3e1a` | 0 rows, `complete` | unchanged |
| `F3e1b` | 4 rows, all `false`, `pending` | 4 rows, all `true`, `complete` |
| `F3e2` | 5 rows (2 inherited + 3 new), `pending` | unchanged |
| `F4` | 2 rows (1 `claim_fn` + 1 new), `pending` | unchanged |
| `F5` | 2 rows (1 `claim_fn` + 1 new), `pending` | unchanged |

Totals: **20 rows → 29** at the end of `F3e1a` (nine new `git_commit` rows), unchanged in
count by `F3e1b`. Kinds: `tool_commit` 9, `claim_fn` 6, `tool_context` 5, `git_commit` 9.

### 3.7 Discovery is not enough — `fenced` must be MEASURED for the new kind

`merge()` recomputes `fenced` **only for `KIND_TOOL_COMMIT`**:

```python
"fenced": (
    (s.path, s.symbol) in fenced_ids
    if s.kind == KIND_TOOL_COMMIT
    else (prev["fenced"] if prev else False)
),
```

Every other kind carries its stored flag, and `test_the_inventory_fenced_flags_match_the_tree`
filters on `row["kind"] == "tool_commit"` too. There is even a test —
`test_non_tool_commit_rows_keep_their_stored_flag` — that **pins this scoping
deliberately**.

So a `git_commit` row added by discovery alone would hold a **hand-written boolean**.
The claim "these sites measure fenced" would be a value someone typed. F3d2 already
fixed this once for `tool_commit` by turning `fenced` into a measurement.

**`F3e1a` extends the measurement to `git_commit`, defined as the same predicate the AST
gate enforces:**

> a `git_commit` site is `fenced` when every mutex acquisition on its path to a sink is
> `acquire_commit_mutex_at_generation`, and none is `acquire_commit_mutex_live`.

Deriving the gate and the measurement from **one function** is deliberate. F3d3a found a
hole where discovery and measurement disagreed about what a raw commit *is* — a
`begin`-only effect owned no inventory row at all, so nothing could notice it missing.
Two predicates that must agree will eventually not; one predicate cannot disagree with
itself.

**Two consequences the plan must carry:**

1. **`test_non_tool_commit_rows_keep_their_stored_flag` changes.** It asserts the old
   scoping and will be wrong once `git_commit` is measured. It is **narrowed** to the
   kinds that genuinely keep a stored flag (`claim_fn`, `tool_context`), not deleted —
   the coverage migrates.
2. **`_SLICE_FOR_KIND` needs an entry, and the default is a trap.**
   `_DEFAULT_SLICE = "F3d2"`, which is **`complete`**. A newly discovered `git_commit`
   site falling through to it would arrive `fenced: false` inside a `complete` slice and
   redden the gate for a reason unrelated to the change that introduced it. The new kind
   maps to **`UNCLASSIFIED`**, which `inventory.py` documents as the value "the gate
   REFUSES … so a regeneration cannot quietly admit an unreviewed effect site." Proven by
   adding a git-committing function and watching the gate refuse it — not asserted.

## 4. The rule

> **Every mutex acquisition on a path that reaches a git sink must be
> `acquire_commit_mutex_at_generation`, carrying the generation the actor believed when
> it decided to act — taken before that path's first effect of any kind.**

"Before its first effect of any kind" is `2026-08-18` §3.5's phrasing, and revision 2
exists because revision 1 wrote that rule and then scoped against it.

### 4.1 What counts as an effect

A DB write, a git commit, a filesystem write, **or a room mutation**.
`store.append_update` writes a `yjs_updates` row and mutates the shared `Doc`; it is an
effect by any reading. This matters because it is what the kernel does *between* taking
the mutex and reaching the sink.

### 4.2 Enclosing acquisitions — the defect revision 1 shipped

`checkpoint_artifact` is a **storage choke point, not the effect boundary.** Ten call
sites funnel through it, and revision 1 argued that converting it alone therefore covered
them. It does not. Five kernel operations and two spec routes acquire the mutex
themselves, at an outer boundary, and mutate before the inner call:

| Enclosing function | Outer mutex | First effect | Sink reached at | Slice |
|---|---|---|---|---|
| `kernel.apply_citation_to_room` | `kernel.py:136` | `append_update` `:172` | `checkpoint_artifact` `:174` | `F3e1b` |
| `kernel.kernel_accept_prose` | `kernel.py:208` | `append_update` `:290` | `checkpoint_artifact` `:294` | `F3e1b` |
| `kernel.kernel_init_spec` | `kernel.py:325` | `append_update` `:339` | `checkpoint_artifact` `:343` | `F3e1b` |
| `kernel.kernel_accept_citation` | `kernel.py:369` | (delegates) | via `apply_citation_to_room` | `F3e1b` |
| `kernel.apply_annotation_insert_to_room` | `kernel.py:443` | `append_update` `:520` | `checkpoint_artifact` `:524` | `F3e1b` |
| `workspaces.post_lock_spec` | `:651` | spec-lock write | `checkpoint_artifact` `:665` | `F3e1b` |
| `workspaces.post_unlock_spec` | `:739` | spec-unlock write | `checkpoint_artifact` `:746` | `F3e1b` |

**Left unconverted, each of these passes `_live` at G after a delete-then-restore, writes
a `yjs_updates` row and mutates the live room, and only discovers staleness at the inner
checkpoint** — by which time the room has already diverged and a DB row is in the
transaction. The inventory would be green.

The existing re-entrancy comment at `kernel.py:203-206` is what makes this safe *today*
(PostgreSQL advisory xact locks are re-entrant within a transaction, so the inner
acquisition is a no-op). Re-entrancy is exactly why the outer acquisition is the one that
decides, and therefore the one that must carry G.

**`F3e1b` converts all seven enclosing acquisitions**, not only the four row-owning
sinks. They do not each get an inventory row — the row stays anchored to the sink caller
— but they are covered by a dedicated gate:

**The no-`_live`-reaches-a-sink gate.** Build the static call graph over `src/` and
assert: for every function that transitively reaches a declared sink, no
`acquire_commit_mutex_live` call appears anywhere on that path. Unconverted sites are
carried in an explicit pin list (the `TOOLS_NOT_YET_CONVERTED` pattern), so an entry
leaving the list is a reviewed diff.

This gate is what makes the choke-point convenience safe: one row, one conversion, but a
proof that covers every path into it.

## 5. Enforcement and proof

### 5.1 The AST gate

For every `git_commit` site and every enclosing acquisition: the function calls
`acquire_commit_mutex_at_generation`, does **not** call `acquire_commit_mutex_live`, and
the mutex call lexically precedes the first effect.

The lexical-precedence check is weaker than the real requirement (it cannot see a call
inside a helper, and lexical order is not execution order under branching). It is a cheap
ratchet, not the proof. The proof is §5.2.

**No assertion here may be satisfiable by a comment.** The project has shipped that
mistake four times — most recently in F3d3b, where a new test's `or` clause admitted a
substring match against `ast.dump`, and before that in F3d3a, where a drift test asserted
a constant's *name* appeared in `inspect.getsource` and passed under the mutation because
the comment beside the code contained the string. Every assertion keys on parsed AST call
nodes, never on source text.

### 5.2 The two ordered lock cases, per converted site

`2026-08-18` §9 ("Every effect operation is proven by TWO ORDERED LOCK CASES") mandates
these for F3b and F3d–F3f:

1. **Case A — effect first, lifecycle second.** The effect takes the mutex at G and
   commits; the delete/restore blocks and proceeds after. The effect's write **lands**,
   and that is correct: it was live when it ran. Case A is what proves the fence does not
   simply refuse everything.
2. **Case B — lifecycle first, effect second.** Delete+restore commits to G+2, then the
   effect acquires the mutex, evaluates against the new generation, and lands **nothing**.

Mechanism 2 takes the exclusive mutex rather than a shared lease, so the deadlock that
killed `2026-08-18` revision 7's straddle test does not apply — both cases are decided by
which side acquires the mutex first.

**Case B needs git-specific assertions.** A proof that inspects only rows passes while
bytes are already on disk — the failure mode `2026-08-18` §7 names for F3e. And §9 is
explicit that **an unchanged HEAD is not enough**: a stale implementation can dirty the
working tree without committing. Each Case B test asserts, on the workspace repo:

- `git rev-parse HEAD` unchanged;
- `git status --porcelain` empty;
- **file contents byte-identical** — cleanliness alone passes an implementation that
  wrote and then reverted;
- **and, for the kernel paths, no `yjs_updates` row and an unmutated room `Doc`** —
  §4.1's effects. A test that checked only git would pass the exact defect §4.2 describes.

**The compensation must be disabled for each per-site test** (`2026-08-18` §9, per
`2026-08-17` §9). Cancel-on-delete is still in the tree and masks staleness for
statements touching the same row; a conversion test that passes with the fence removed is
measuring the compensation, not the fence. This is the technique that exposed Slice C1's
decorative tests.

### 5.3 Mutation proofs for the gates themselves

Both gates are new, so each is proven by breaking it:

- Reverting one converted site to `acquire_commit_mutex_live` fails the AST gate.
- Reverting one **enclosing** acquisition fails the no-`_live`-reaches-a-sink gate. This
  is the one that would have caught revision 1's defect, so it is not optional.
- Hand-editing a `git_commit` row's `fenced` to `true` does not survive regeneration.
- Adding a git-committing function leaves it `UNCLASSIFIED` and the gate refuses it.
- Adding a `subprocess` git-commit outside the declared sinks fails the
  sink-completeness guard.

Each must fail **on its named assertion for the named reason** — not by aborting
collection. `tests/fencing` is *not* insulated from the app import chain (the root
`tests/conftest.py:135` autouse teardown imports the app for every test in the repo), so
a broken import shows up as mass teardown **ERRORS with zero FAILED**. Every
stop-the-run judgement in the plan keys on **FAILED**, never on ERROR.

## 6. Which path actually runs, and why it changes the target

Both citation routes fork on `is_collab_enabled` (`web/api/workspaces.py:476-482` and
`:1027-1033`). **Collab is ON in production** (enable-on-touch, shipped in D4 `0c53a72`).
So in production today:

- the live citation write path is `collab/kernel.py` → `checkpoint_artifact`;
- `_insert_citation_locked` and `insert_citation_for_user` are the flag-off paths, and
  their own comments describe them as *"retiring rather than maintained"* — with collab
  enabled their severance recheck raises `CollabSeveranceConflict` → 409.

This is why §3.1 chose repo-wide detection, and why §4.2's enclosing acquisitions matter
more than the sink: **the enclosing kernel operations are the live path**. A slice that
fenced only the in-effect-scope site, or only the shared helper, would report the citation
half done while production's path stayed exposed.

The flag-off paths still convert. They are reachable (the flag can be off for a workspace
never touched), and leaving them out would put entries in the pin list with no reason.

## 7. The generation, from capture to sink

Revision 1 stopped at "`checkpoint_artifact` takes G as a parameter — ten call sites."
That is the last link, not the chain. `acquire_commit_mutex_at_generation` requires the
caller to know G, and **G must be the value the actor believed when it decided to act**.
Reading G later — immediately before the mutex, or under it — adopts the *restored*
generation and makes the assertion compare a value against itself. A test driving
`checkpoint_artifact` directly cannot catch that, because it supplies G itself.

### 7.1 The chain per entry point

| Entry point | Capture point | Parameter path | Enclosing acquisitions receiving the same G |
|---|---|---|---|
| `POST …/cite` (collab) | route's `require_owner` → `Workspace` | route → `direct_cite_via_kernel` → `apply_citation_to_room` → `checkpoint_artifact` | `apply_citation_to_room` |
| `POST …/proposed-changes/{id}/accept` (collab, prose) | route's owner load | route → `accept_via_kernel` → `kernel_accept_prose` → `checkpoint_artifact` | `kernel_accept_prose` |
| same (collab, citation) | route's owner load | route → `accept_via_kernel` → `kernel_accept_citation` → `apply_citation_to_room` → `checkpoint_artifact` | `kernel_accept_citation`, `apply_citation_to_room` |
| `POST …/spec/init` (collab) | route's owner load | route → `init_spec_via_kernel` → `kernel_init_spec` → `checkpoint_artifact` | `kernel_init_spec` |
| `POST …/annotations/{id}/insert` (collab) | route's owner load | route → `insert_annotation_via_kernel` → `apply_annotation_insert_to_room` → `checkpoint_artifact` | `apply_annotation_insert_to_room` |
| `POST …/spec/lock` | route's `require_owner` → `ws` | route → `checkpoint_artifact` | `post_lock_spec` itself |
| `POST …/spec/unlock` | route's `require_owner` → `ws` | route → `checkpoint_artifact` | `post_unlock_spec` itself |
| `POST …/sections/{i}` (save) | route's owner load → `Workspace` | route → `save_section` | `save_section` itself |
| `POST …/cite` (flag-off) | route's owner load → `Workspace` | route → `insert_citation_for_user` | `insert_citation_for_user` itself |
| accept (flag-off, citation) | route's owner load | route → `accepting.py` → `_insert_citation_locked` | `_insert_citation_locked` itself |
| checkpoint on evict / handover / `save_version` | see §7.3 | — | `checkpoint_artifact` itself |

### 7.2 The plumbing gap this exposes

**`direct_cite_via_kernel` (`collab_cite.py:31`), `init_spec_via_kernel`,
`insert_annotation_via_kernel` and `accept_via_kernel` (`kernel_accept.py:57`) carry only
`workspace_id: uuid.UUID`.** They open their own sessions through `session_factory`
inside `_body()`, and the route's authorization `Workspace` — the actual capture point —
is discarded before they are entered.

So the plumbing is not a ten-call-site signature change on one helper. It is a
`generation: int` parameter added to **four orchestrators** as well, threaded from the
route's already-loaded `Workspace.lifecycle_generation`. This is the largest mechanical
piece of `F3e1b` and the plan enumerates it per call site.

**Route-driven sites capture from the instance they already hold.** `save_section`,
`insert_citation_for_user` and the two spec routes take a `Workspace` **instance**, loaded
for authorization *before* the mutex. That load is the capture point, and the gap between
it and the mutex is real: the handler authorizes, does request-shaped work, then blocks on
a mutex another writer may hold for seconds. `G` comes from
`workspace.lifecycle_generation` on that instance.

This is the mirror image of the Slice C1 defect fixed in `701fbb7`, where `session.get()`
returned an identity-map instance loaded *before* the mutex and carried the pre-race
generation. There it was the bug; here the pre-block value is precisely the claim being
checked. **What must not happen is re-reading G under the mutex.**

### 7.3 Maintenance callers have no actor

Three `checkpoint_artifact` callers are not acting on anyone's behalf: the evict hook
(`collab/permissions.py:58`), the kill-switch handover (`collab/handover.py:40`), and the
periodic tail checkpoint (`collab/checkpoint.py:210`). There is no earlier belief to
preserve — they are reacting to current state.

These read G **under the mutex** and pass it, making the assertion a deliberate no-op, and
the plan says so at each site rather than leaving a reader to wonder. What they gain is
the `WorkspaceDeleted` refusal they already have, plus uniformity: the no-`_live` gate
needs no exemption for them.

### 7.4 What the tests must drive

**At the semantic boundary, not the helper.** A test that calls `checkpoint_artifact` with
a hand-supplied G proves nothing about capture. Each entry point in §7.1 gets a runtime
race: capture at G (through the real route or orchestrator), commit a delete+restore to
G+2, then let the operation proceed. It must raise and land nothing — including no
`yjs_updates` row (§5.2).

The nested case gets its own test: an inner acquisition handed a **different** G than the
outer asserted must raise rather than proceed, so a future caller that re-reads instead of
threading is caught.

## 8. The stale-generation outcome matrix

Revision 1 asserted that `WorkspaceGenerationStale` "maps to each caller's existing
deleted-workspace outcome". **It maps to nothing today.** Verified:

- `WorkspaceGenerationStale` is defined in `ports/errors.py:13` as a bare `RuntimeError`.
- `WorkspaceDeleted` is defined in `drafting/locking.py:163`, also a bare `RuntimeError`.
  `main.py:541` records explicitly that **neither is a subclass of the other**.
- The global handler at `main.py:514` registers `WorkspaceDeleted` only.
- Maintenance paths classify only deletion as benign: `handover.py:82` and
  `permissions.py:79` catch `WorkspaceDeleted` in their skip clauses, and
  `checkpoint.py:219` does the same.

Left unaddressed, a stale generation becomes a **500**, an unnecessary room eviction, or
an **error-level maintenance log** — and the room paths would evict for a condition that
is not an error.

**`F3e1b` ships an explicit per-caller matrix**, and no site converts before its row is
filled in:

| Caller class | Deleted today | Stale must become |
|---|---|---|
| Owner write routes (save, cite, accept, spec lock/unlock) | 404 via the global handler | **404**, same body — registered by extending the `main.py:514` handler to both types |
| Collab kernel ops | `_CLEAN_ACCEPT_ERRORS` (`kernel_accept.py:43`) → clean error, no room eviction | **clean error, no eviction** — added to that list |
| Maintenance (evict, handover, tail checkpoint) | benign skip, no error log | **benign skip** — added to the same `isinstance` tuples |
| Agent tool paths (`_insert_citation_locked`) | `DELETED_ERROR_MESSAGE` | `DELETED_ERROR_MESSAGE` — a restored workspace is, from the actor's view, gone |

The last row is a judgement worth stating plainly: the agent is told "deleted" for a
workspace that exists again at a later generation. That is the honest message for the
actor's situation — the thing it was working on is not there any more — and it matches the
mapping F3d3b gave the propose sites, where `WorkspaceGenerationStale` returns
`DELETED_ERROR_MESSAGE` (`tools/citations.py`, `tools/proposal.py`). **Every
one of these mappings is asserted by a test**, because a mapping nobody exercises is a
mapping that drifts.

## 9. Where this departs from `2026-08-18` §7, and why

§7's F3e section says F3e "owns every effect that spans the DB and either git or the
filesystem" and frames the citekey row's fix as "an operation [that] owns the whole
accept-time sequence in `_insert_citation_locked`" — which reads as a port operation
swallowing the accept.

**Two findings from reading the code changed that.**

1. **The accept path's atomicity is deliberate and a port would break it.**
   `accepting.py`'s docstring is explicit: `owns_txn=False` is what keeps the citation
   git/ledger write and the `ProposedChange` status flip in **one** transaction
   ("Atomicity is NOT automatic — `owns_txn=False` is what makes it single-transaction").
   A port operation owning its own commit (`2026-08-18` §2) splits them.
2. **The sequence §7 names is the retiring one** (§6). An operation built around
   `_insert_citation_locked` would not cover the path production runs.

Ports exist so that **tools** never hold a `Session` (`2026-08-18` §2: "Tools receive
capabilities, never handles"). The git-committing sites are **services** — route
handlers, the collab write kernel, the accept path — and a service legitimately holds a
session. `2026-08-18` §3.5 already frames mechanism selection as a property of the lock
rather than the port: its third mechanism reads "where the caller ALREADY holds the
workspace row under FOR UPDATE, **do not call this at all** — assert the generation on
that already-locked row."

**So: ports are for tools; mechanisms are for effects.** A service can be
mechanism-2-correct with no port in sight.

**This document amends `2026-08-18` §7**: its F3e section splits into `F3e1a`, `F3e1b`
and `F3e2`, and its expectation that the citekey row's fix is a port operation is
corrected to a mechanism conversion. The amendment is written into that spec file as part
of `F3e1a`'s first task, so the two documents do not disagree in the tree.

## 10. `_write_citekey_cache` — carried, not claimed

The row reassigned from F3d3 to F3e in F3d3b stays `fenced: false` through both
sub-slices, and the slice says so rather than letting a reader assume otherwise.

Its `owns_txn=True` branch — the `session.commit()` at `citations/core.py:521` that is the
only reason the site is discovered — is reached solely from `insert_citation_impl`, which
**has no production caller**. It was de-allowlisted as a tool in `855d59ae`, and only six
test files drive it.

**Deleting that branch would make the row disappear, and that is precisely what must not
happen.** It would remove a discovered site while the live hazard remained, and
`inventory.py`'s comment on `F3D_IDENTITIES` forbids it: "Deleting an entry here is not a
way to make a test pass. The entry leaves when the identity is genuinely gone from the
design."

The row becomes fenced when the sequence containing it is mechanism-2 correct **and** the
`tool_commit` measurement (a port write and no raw commit) can see it. `F3e1b` delivers
the first half; the second is a measurement question shared with the ingest slice, which
faces the same "an effect outside `tools/` needs a measurement that is not port-shaped"
problem. **`F3e2` owns it.**

## 11. Deferred, with owners

- **`F3e2`** — the ingest pipeline, filesystem effect detection (`write_atomic`,
  `write_text`, `delete_one`), the half-ingest stale-generation answer, the
  `references.py` reads, `_write_citekey_cache`'s measurement, and the three `F3e2` rows
  from §3.4.
- **`F4`** — `review/pass_runner.py:run_review_pass`, the git site with the longest
  capture-to-effect gap (it claims at `claimed_at_generation`, runs LLM work, then
  commits). It belongs with the job families.
- **`F5`** — `phases/setup.py:Phase0Worker._init_repo`, discovered in `F3e1a` and
  converted with Phase 0.
- **`F3f`** — the pins. Nothing in F3e-1 retires one: all five exempt modules still name
  `repo_dir`.
- **Deferred, no owner yet:** whether accepting a `ProposedChange` created at G should be
  refused after a delete/restore to G+2. `ProposedChange` carries no generation column
  (the five job tables carry `claimed_at_generation`; it does not), and adding one here
  would smuggle a product decision into a fencing slice.
- **Open, pre-existing, not blocking:** `_safe_tool`'s `ports is None` `RuntimeError`
  (`tools/registry.py:292-301`) raises outside the try/except that keeps exceptions out of
  MCP handlers. Unreachable today.

## 12. Testing

Beyond §5's gates, proofs and mutations:

- **Behaviour parity is the bar.** Converted sites must be byte-identical in behaviour
  except for the sanctioned change in §8's matrix. Anything else that has to change to
  make a test pass is a behaviour change: report it, do not absorb it.
- **Full suite** `uv run pytest -n 16 --dist loadfile -q`; never `-n auto` (resolves to 72
  here) and never `--dist loadgroup`. `ruff check` is a gate; `ruff format` is not.

## 13. Success criteria

**`F3e1a`:**

1. Nine `git_commit` sites discovered, including `_init_repo` via `_run_git`, and each
   allocated to a named slice. Zero converted; every row `fenced: false`.
2. A `subprocess` git-commit outside the declared sinks fails the sink-completeness
   guard — demonstrated.
3. An aliased sink import is still discovered — demonstrated by fixture.
4. A hand-edited `fenced: true` does not survive regeneration — demonstrated.
5. A new git-committing function arrives `UNCLASSIFIED` and the gate refuses it —
   demonstrated.
6. **No production behaviour changes.** Suite green with no site converted.

**`F3e1b`:**

7. `acquire_commit_mutex_at_generation` has production callers for the first time.
8. All seven enclosing acquisitions (§4.2) converted, and reverting any one fails the
   no-`_live`-reaches-a-sink gate on its named assertion — demonstrated.
9. For each entry point in §7.1, a delete+restore between **capture and effect** leaves
   HEAD unchanged, the worktree clean, file contents byte-identical, and **no
   `yjs_updates` row** — with compensation disabled.
10. Every §8 mapping asserted by a test; no path yields a 500, a spurious eviction, or an
    error-level maintenance log.
11. Pins stay at 5, and the slice's own tests say so.
