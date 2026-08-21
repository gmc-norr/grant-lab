# Fence F3e-1 — the mechanism-2 gate for git effects

**Status:** design, **revision 5** (2026-08-21). Successor to F3d3b (`eb7d4a34`).
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
>
> **Revision 3 — six further defects, all verified.** Revision 2's `fenced` predicate was
> **vacuously true for a sink with no mutex at all**: "every acquisition is
> `_at_generation` and none is `_live`" is satisfied by zero acquisitions, and
> `bibliography.build_bibliography` and `pass_runner.run_review_pass` take no mutex —
> both would have measured `fenced: true` while completely unfenced, contradicting
> `F3e1a`'s own requirement that all nine rows be `false` (§3.7). Revision 2's §7.3 told
> maintenance callers to "read G under the mutex and pass it", which **Primitive B cannot
> do** — `generation` is a required keyword argument consumed before the mutex is taken
> (§7.3). Revision 2's capture table said "owner load" for routes that use
> `resolve_access`, which would have **revoked collaborator write access** — a
> permissions regression inside a fencing slice — and filed the actor-driven
> `save_version` route under maintenance (§7.1, §7.3). Revision 2 named
> `store.append_update` as the first effect; the shared `Doc` is mutated one line earlier
> (§4.1). The outcome matrix named one clean-error tuple; there are **three** (§8). And
> the sink-completeness claim was broader than its guard (§3.2).
>
> **Revision 4 — ten more, from a detached multi-agent review.** No High findings and no
> architectural ones; the split was confirmed drawn in the right place. But four
> **unenumerated machinery updates** would each have turned `F3e1a` red on its own "suite
> green, nothing converted" criterion: a hardcoded `== 20` row count
> (`test_inventory_merge.py:113`), a `CATEGORIES_BY_KIND` entry the new kind requires
> plus a legal category per row, an `F3D_IDENTITIES` tuple and a test name/assertion that
> both hardcode `"F3e"`. Revision 3 also **half-fixed its own bug**: §4.1 was corrected to
> name the `Doc` mutation as the first effect while §4.2's table still said
> `append_update` — and the table is what a plan author builds the gate from (§4.2).
> Four more were wrong citations or wrong tuples (§7.2, §8, §11).
>
> **Revision 5 — structural, and it SHRINKS the design.** Four review rounds kept finding
> defects in one section, §7, and the reason was structural rather than careless: tools
> have a carrier for the generation (`Invocation` → `FenceStamp`, minted once at dispatch,
> frozen), and revisions 1–4 asked **eleven services to reinvent it by hand** with a bare
> `int`. Every §7 finding was a symptom — an impossible capture step, the wrong
> authorization function, bespoke maintenance handling, untyped orchestrator parameters.
> Revision 5 reuses `FenceStamp` for services (verified reusable: its two tool fields are
> already nullable, no git effect reads them, and `stamp.py` is stdlib-only), adds one
> service-side constructor beside `Invocation.stamp()`, and replaces the eleven-row
> capture table with **one rule**. Four previously-found defects become unwritable, and
> "capture early" stops being prose an implementer must remember and becomes a **static
> gate**: a generation passed to a mechanism-2 acquisition must come from a stamp.

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

**`F3e1a` — the carrier, the detector and the inventory. Converts nothing.**
The service-side `FenceStamp` constructor (§7.0.2) and the gate that mechanism-2
generations come from a stamp (§7.0.3); canonical git-sink detection, the
sink-completeness guard, the `git_commit` kind, the `fenced` **measurement** for that
kind, the `UNCLASSIFIED` slice default, the `CATEGORIES_BY_KIND` entry, and the AST gate
armed with all nine sites pinned as unconverted. Every `git_commit` row lands
`fenced: false`. **No production behaviour changes at all** — the constructor has no
callers yet, exactly as F3b's ports had none.

Putting the carrier in the spine rather than in the conversion slice is the whole lesson
of §7.0: F3d1 gave converted tools everything they needed *before* F3d2 converted one, and
the conversions then landed with their derived test counts hitting exactly. F3e1b should
inherit a mechanism, not a paragraph.

**`F3e1b` — the conversions.**
The enclosing-acquisition enumeration, the stamp threaded from mint site to sink, the
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

**Two guards, because one claim was broader than its evidence.** Revision 2 promised that
a third commit mechanism would redden the build, but a `subprocess`-argv guard cannot see
a GitPython commit, an `os.system`, or a dynamically assembled wrapper. Rather than narrow
the claim, the guard is widened to make it true — which is cheap here, because the surface
is genuinely small:

1. **The argv guard.** No `subprocess` invocation in `src/` passes an argv whose first
   git-subcommand token is `commit`, outside a declared sink.
2. **The execution-surface allowlist.** `subprocess` is currently the *only* way this
   repository runs a process — verified: no `os.system`, no `os.popen`, no
   `asyncio.create_subprocess_*`, and no git library (`GitPython`, `pygit2`, `dulwich`) in
   `pyproject.toml` or `uv.lock`. The guard asserts that this remains true. Introducing a
   new execution surface or a git library is then a **reviewed diff** that must extend the
   argv guard to cover it.
3. **No `shell=True` outside a declared sink.** A list-walking argv guard cannot see
   `subprocess.run("git commit -m x", shell=True)` — the command is one string with no
   element to index, so clause 1 is blind to it. No `shell=True` call exists in `src/`
   today (the only match is a *comment* at `gitio.py:56`), so this closes a hole rather
   than fixing a miss.

Together these make the claim honest: every way this repository can invoke git is
enumerated, and adding a new way stops the build.

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
field, so regeneration will not move them: the reassignment starts as a hand-edit of
`fence_inventory.json`, proven by regenerating into a temporary copy and comparing it to
the committed file. **Not by `git diff --exit-code`** — on a file the task deliberately
edited, that can never pass. F3d3b shipped that mistake and had it caught twice.

**But it is NOT a single-file edit, and revision 3 said it was.** Three Python assertions
hardcode the strings this slice changes, and each reddens `F3e1a` on its own "suite green,
nothing converted" criterion. All four edits land in the **same commit**:

| File | What | Change |
|---|---|---|
| `tests/fencing/fence_inventory.json` | the two rows' `slice` | `"F3e"` → `"F3e2"` |
| `tests/fencing/inventory.py:336` | `F3D_IDENTITIES` holds `("F3e", "tool_commit", "_write_citekey_cache")`; `test_every_f3d_identity_is_still_in_the_inventory` compares it against the live rows | the tuple's slice → `"F3e2"` |
| `tests/fencing/test_fence_coverage.py:408-421` | `test_the_ingest_row_is_untouched_and_still_f3e` asserts `row["slice"] == "F3e"` | assertion, **name** and docstring → `F3e2` |
| `tests/fencing/test_inventory_merge.py:113` | `assert len(after) == 20`, driven over the real tree | → `29` (the nine new `git_commit` rows) |

**`F3D_IDENTITIES` is edited here, and that does not contradict §10.** Its comment forbids
*deleting* an entry to make a test pass. This **modifies** an entry's slice to follow a
reallocation a human made — the identity is intact, and the row it names still exists. A
reader who takes §10 to mean the constant is untouchable will leave `F3e1a` red.

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

> a `git_commit` site is `fenced` when **every executable path from it to a sink passes
> through at least one `acquire_commit_mutex_at_generation` call, positioned before that
> path's first effect**, and no path contains an `acquire_commit_mutex_live`.

**The "at least one" clause is load-bearing, and revision 2 omitted it.** "Every
acquisition is `_at_generation` and none is `_live`" is **vacuously true of a path with no
acquisitions**. Two sinks are exactly that shape today: `citations/bibliography.py`
contains no mutex call at all, and `review/pass_runner.py` has none either (its only
`commit mutex` mention is a comment). Under revision 2's predicate both would have
measured `fenced: true` — reporting two entirely unfenced git effects as fenced, and
simultaneously reddening `F3e1a`'s requirement that all nine rows land `false`.

A missing fence and a correct fence must not be the same measurement. The corrected
predicate is **positive**: something must be there, in the right place, and nothing wrong
may be.

Deriving the gate and the measurement from **one function** is deliberate. F3d3a found a
hole where discovery and measurement disagreed about what a raw commit *is* — a
`begin`-only effect owned no inventory row at all, so nothing could notice it missing.
Two predicates that must agree will eventually not; one predicate cannot disagree with
itself.

**The new kind also needs a `category`, and revision 3 never mentioned it.** The row shape
is `{kind, path, symbol, category, fenced, slice}` and two gates police that field:

- `CATEGORIES_BY_KIND` (`inventory.py:31-35`) has entries for `tool_commit`,
  `tool_context` and `claim_fn` only. `test_each_category_is_legal_for_its_kind` does
  `allowed = CATEGORIES_BY_KIND.get(row["kind"])` then asserts `allowed is not None`.
  Nine `git_commit` rows with no entry **fail** it.
- `test_no_entry_is_unclassified` forbids `category == UNCLASSIFIED` on any row — while
  `merge()` (`inventory.py:284`) assigns exactly that to a row with no `prev`. So the nine
  rows cannot be left to default.

`F3e1a` therefore defines **`CATEGORIES_BY_KIND[KIND_GIT_COMMIT] = frozenset({"semantic",
UNCLASSIFIED})`** and gives all nine rows the explicit category **`semantic`**.

That choice is a design decision, not plumbing: `semantic` only, with **no analogue of
`caller_fenced`**. A `tool_commit` may claim `caller_fenced` because a caller's transaction
can fence it; a git effect cannot be fenced by someone else's transaction — the mutex is
taken by the function itself or the effect is unfenced. Leaving the exemption out means a
`git_commit` row has no way to declare itself somebody else's problem.

**Note the two `UNCLASSIFIED` uses are different fields and must not be conflated.**
`_SLICE_FOR_KIND` maps the new kind's **slice** to `UNCLASSIFIED` so an undeclared site
stops the build; the **category** must never be `UNCLASSIFIED`.

**Three further consequences the plan must carry:**

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

**Revision 2 named the wrong one.** It said `store.append_update` mutates the shared
`Doc`; it does not — `store.append_update` (`collab/store.py:19-42`) only stages a
`YjsUpdate` row. **The `Doc` is mutated one step earlier**, by
`apply_pm_section_to_fragment` (`kernel.py:169-172`) and `apply_pm_json_to_fragment`
(`kernel.py:338-339`). Those calls are the true first effect on the kernel paths, and they
are what the lexical gate (§5.1) must recognise as the boundary the mutex has to precede.

Getting this wrong is not cosmetic: a gate anchored on `append_update` would accept an
implementation that mutated the live room before acquiring the mutex, which is precisely
the divergence §4.2 exists to prevent.

### 4.2 Enclosing acquisitions — the defect revision 1 shipped

`checkpoint_artifact` is a **storage choke point, not the effect boundary.** Ten call
sites funnel through it, and revision 1 argued that converting it alone therefore covered
them. It does not. Five kernel operations and two spec routes acquire the mutex
themselves, at an outer boundary, and mutate before the inner call:

| Enclosing function | Outer mutex | First effect | Sink reached at | Slice |
|---|---|---|---|---|
| `kernel.apply_citation_to_room` | `kernel.py:136` | `apply_pm_section_to_fragment` `:169` | `checkpoint_artifact` `:174` | `F3e1b` |
| `kernel.kernel_accept_prose` | `kernel.py:208` | `apply_pm_section_to_fragment` `:274` | `checkpoint_artifact` `:294` | `F3e1b` |
| `kernel.kernel_init_spec` | `kernel.py:325` | `apply_pm_json_to_fragment` `:338` | `checkpoint_artifact` `:343` | `F3e1b` |
| `kernel.kernel_accept_citation` | `kernel.py:369` | (delegates) | via `apply_citation_to_room` | `F3e1b` |
| `kernel.apply_annotation_insert_to_room` | `kernel.py:443` | `apply_pm_section_to_fragment` `:517` | `checkpoint_artifact` `:524` | `F3e1b` |
| `workspaces.post_lock_spec` | `:651` | spec-lock write | `checkpoint_artifact` `:665` | `F3e1b` |
| `workspaces.post_unlock_spec` | `:739` | spec-unlock write | `checkpoint_artifact` `:746` | `F3e1b` |

**The "First effect" column names the `Doc` mutation, not `store.append_update`** — see
§4.1. Revision 3 corrected §4.1's prose and left this table pointing at `append_update`,
which is the column a plan author builds the §5.1 gate boundary from. Anchoring the gate
one line late would accept an implementation that mutated the live room before taking the
mutex: the exact hollow gate §4.1 warns about, in the table §4.1 is warning about.

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

## 7. The carrier, and the generation from capture to sink

### 7.0 Services get a carrier, not eleven sets of instructions

**Revisions 1–4 kept getting this section wrong, and the reason was structural.**
`acquire_commit_mutex_at_generation` needs G, and G must be the value the actor believed
when it *decided to act* — not a value read later, which adopts the restored generation
and makes the assertion compare a value against itself.

Tools already have machinery for exactly that. F3a/F3b built `Invocation` → `FenceStamp`
(`ports/stamp.py`): minted once at dispatch, frozen, carried with the actor. There is
exactly **one** place in the codebase that can build one — `invocation.py:75`.

Revisions 1–4 asked **eleven services** to reinvent it by hand, threading a bare `int`.
That is where the review findings concentrated, and they were all the same shape:

- a capture step **no API can perform** ("read G under the mutex and pass it");
- a capture table naming **the wrong authorization function**, which would have revoked
  collaborator write access;
- maintenance callers needing a bespoke special case;
- four orchestrators needing an untyped parameter nothing could check.

Prose describing eleven bespoke capture points is where enumeration errors live. **The
fix is a carrier, and the carrier already exists.**

### 7.0.1 `FenceStamp` is reused, not re-invented

Verified against the tree, three ways:

- Its two tool-ish fields are **already nullable** — `active_turn_job_id: uuid.UUID | None`
  and `origin_job_id: uuid.UUID | None` — and `None` is a documented legitimate value:
  `registry.py` leaves `origin_job_id` `None` for interactive contexts *by design*.
- **Nothing a mechanism-2 git effect does reads them.** Every `stamp.origin_job_id`
  consumer in `port_impl/effects.py` is on a `submit_*` (background job row) or `propose_*`
  (durable attribution) path. A git effect needs `workspace_id` and `generation`, full stop.
- `ports/stamp.py` is **stdlib-only** (`uuid`, `dataclasses`), so a route, the kernel or a
  maintenance job can import it with no layering problem.

So `FenceStamp` is not tool-shaped, and F3e-1 does not mint a sibling type.

### 7.0.2 Exactly two ways to build one, and each REFUSES rather than defaults

`F3e1a` adds the second constructor beside `Invocation.stamp()`:

```python
def stamp_for_workspace(workspace: Workspace) -> FenceStamp:
    """The fencing identity of an actor holding a Workspace it just loaded.

    The service-side twin of Invocation.stamp(). The job fields are None because
    a request or a maintenance pass is not a background job — the same `None`
    registry.py already writes for interactive contexts.
    """
    if workspace.lifecycle_generation is None:
        raise UnfencedInvocation(workspace.id)
    return FenceStamp(
        workspace_id=workspace.id,
        generation=workspace.lifecycle_generation,
        active_turn_job_id=None,
        origin_job_id=None,
    )
```

**The refusal is copied deliberately.** `Invocation.stamp()` raises `UnfencedInvocation`
rather than defaulting, and its docstring gives the reason: *"an actor with no claimed
generation has no belief to fence against, and defaulting would submit an effect against a
generation nobody claimed. Generation 0 is REAL … so this tests for None, never for
falsiness."* Every word of that applies here.

### 7.0.3 What the carrier buys, stated as the rule it replaces

> **Every mechanism-2 acquisition takes its generation from a `FenceStamp`, and a
> `FenceStamp` is minted only where an actor already holds the `Workspace` it is acting on.**

One rule replaces revision 4's eleven-row capture table. Four previously-found defects
become **unwritable**:

| Defect | Why it cannot recur |
|---|---|
| "read G under the mutex" | minting happens where the `Workspace` is held, which is before the mutex by construction |
| wrong authorization function named | the rule names no check — you mint from whatever object the check you *already run* loaded |
| maintenance callers special-cased | they hold a `Workspace` from their pre-screen, so they follow the same rule |
| orchestrators take a bare `int` | they take `stamp: FenceStamp`, which is typed, self-documenting and checkable |

**And it converts a prose discipline into a machine-checkable one.** Revisions 1–4 could
only *say* "capture early". With a carrier, `F3e1a` can add a gate:

> no call to `acquire_commit_mutex_at_generation` may pass a `generation=` argument that is
> not a `FenceStamp` attribute.

That gate rejects `generation=session.get(Workspace, wid).lifecycle_generation` written
inline at the mutex — the late-read defect — statically. A rule the tooling enforces is
worth more than four revisions of prose asking implementers to be careful.

### 7.1 Where each entry point mints its stamp

**No authorization call changes in this slice.** The mint site is "the object the existing
check loaded", whatever that check is. Recorded so an implementer never has to guess, and
because revision 2 guessed wrong here and would have revoked collaborator access:
the collab cite branch authorizes with `resolve_access` (`workspaces.py:1038`, **any member
with write**), while only the flag-off branch of the same route uses `require_owner`
(`:1098`); acceptance uses `require_member`.

| Entry point | Existing check (UNCHANGED) | Mints from | Stamp threaded through |
|---|---|---|---|
| `POST …/cite` (collab) | `resolve_access` → `can_write` | the `Workspace` it loaded | `direct_cite_via_kernel` → `apply_citation_to_room` → `checkpoint_artifact` |
| accept (collab, prose) | `require_member` → `Workspace` | that instance | `accept_via_kernel` → `kernel_accept_prose` → `checkpoint_artifact` |
| accept (collab, citation) | `require_member` → `Workspace` | that instance | `accept_via_kernel` → `kernel_accept_citation` → `apply_citation_to_room` → `checkpoint_artifact` |
| `POST …/spec/init` (collab) | as the route stands | the instance it loaded | `init_spec_via_kernel` → `kernel_init_spec` → `checkpoint_artifact` |
| `POST …/annotations/{id}/insert` | as the route stands | the instance it loaded | `insert_annotation_via_kernel` → `apply_annotation_insert_to_room` → `checkpoint_artifact` |
| `POST …/spec/lock` | `require_owner` → `ws` (`:645`) | `ws` | `post_lock_spec` → `checkpoint_artifact` |
| `POST …/spec/unlock` | `require_owner` → `ws` (`:734`) | `ws` | `post_unlock_spec` → `checkpoint_artifact` |
| `POST …/sections/{i}` (save) | as the route stands | the instance it loaded | `save_section` |
| `POST …/cite` (flag-off) | `require_owner` (`:1098`) | that instance | `insert_citation_for_user` |
| accept (flag-off, citation) | `require_member` | that instance | `accepting.py` → `_insert_citation_locked` |
| `save_version` | `resolve_access` → `can_write` (`collab_checkpoint.py:38`) | the `Workspace` it loaded | `checkpoint_artifact` |
| evict hook / handover / tail checkpoint | none — no actor | the `s.get(Workspace, …)` pre-screen (§7.3) | `checkpoint_artifact` |

**`resolve_access` does not change.** Revision 3 proposed widening it to a 3-tuple and
claimed callers ignoring the extra value would be unaffected; that is false in Python —
all six call sites destructure exactly two values (`workspaces.py:298`, `:351`, `:1038`,
`permissions.py:116`, `collab_checkpoint.py:38`, `collab_ws.py:81`), so a third element
raises at every one, including two unrelated to this slice. The route reads the workspace
itself after the check passes and mints from it. That costs no extra query — the row is
already in this session's identity map — but the argument does not depend on that.

### 7.2 The plumbing this still requires

A carrier removes the *reasoning* from each call site; it does not remove the parameter.

**`direct_cite_via_kernel` (`collab_cite.py:31`), `init_spec_via_kernel`,
`insert_annotation_via_kernel` and `accept_via_kernel` (`kernel_accept.py:57`) carry only
`workspace_id: uuid.UUID`** and open their own sessions inside `_body()`, so the route's
`Workspace` is discarded before they are entered. Each gains a `stamp: FenceStamp`
parameter, as does `checkpoint_artifact` and the seven enclosing acquisitions of §4.2.

That is still the largest mechanical piece of `F3e1b`, and the plan enumerates it per call
site. What changed is that a wrong thread is now a **type error or a gate failure** rather
than a plausible-looking integer.

**Nested acquisition threads the same stamp inward.** `kernel_accept_prose` acquires and
then calls `checkpoint_artifact`, which acquires again — safe today because PostgreSQL
advisory xact locks are re-entrant within a transaction (`kernel.py:203-206`). The inner
call receives **the same stamp object**, never a re-mint: re-minting under a held mutex
would read the same value and so hide a caller that threaded the wrong one. A test asserts
that an inner acquisition handed a *different* stamp raises rather than proceeding.

### 7.3 Maintenance callers mint from their pre-screen — the same rule

Three `checkpoint_artifact` callers act for no one: the evict hook
(`collab/permissions.py:58`), the kill-switch handover (`collab/handover.py:40`), and the
periodic tail checkpoint (`collab/checkpoint.py:210`).

**Revision 2 told them to "read G under the mutex and pass it", which Primitive B cannot
do** — `generation` is a required keyword argument consumed only after the mutex is
already held (`fencing.py:115-121`). Under the carrier the question does not arise: all
three already run the *identical* pre-screen immediately before calling —
`ws = s.get(Workspace, workspace_id)` then a `soft_deleted_at` check
(`permissions.py:53-55`, `handover.py:36-38`, `checkpoint.py:205-207`). **That instance is
the mint site**, exactly as a route's access check is. No special case, and no exemption
from the gate.

A delete/restore committing between the pre-screen and the mutex then produces a
`WorkspaceGenerationStale` refusal, which for these three is **correct rather than a
regression**: all three already treat `WorkspaceDeleted` as a benign skip (§8), and a
workspace that moved generation under a maintenance pass is one that pass should skip and
pick up next iteration.

### 7.4 What the tests must drive

**At the semantic boundary, not the helper.** A test that calls `checkpoint_artifact` with
a hand-built stamp proves nothing about minting. Each entry point in §7.1 gets a runtime
race: mint at G through the real route or orchestrator, commit a delete+restore to G+2,
then let the operation proceed. It must raise and land nothing — no git commit, a clean
worktree, byte-identical files, and **no `yjs_updates` row** (§5.2).

Plus two the carrier makes possible:

- **`stamp_for_workspace` refuses a workspace with no generation**, mirroring
  `Invocation.stamp()`'s `UnfencedInvocation` — asserted, not assumed.
- **The static gate rejects an inline late read.** A fixture writing
  `generation=session.get(Workspace, wid).lifecycle_generation` at the mutex must fail
  §7.0.3's gate. This is the defect four revisions could only warn about in prose.

## 8. The stale-generation outcome matrix

Revision 1 asserted that `WorkspaceGenerationStale` "maps to each caller's existing
deleted-workspace outcome". **It maps to nothing today.** Verified:

- `WorkspaceGenerationStale` is defined in `ports/errors.py:13` as a bare `RuntimeError`.
- `WorkspaceDeleted` is defined in `drafting/locking.py:163`, also a bare `RuntimeError`,
  and nothing relates the two types. (Revision 3 cited `main.py:541` for this; that comment
  is about **`WorkspaceRetired`** not subclassing `WorkspaceDeleted`. The *pattern* it
  records is the relevant precedent — a sibling bare `RuntimeError` needs its own
  registration because "Starlette cannot reach the handler above" — but it is precedent,
  not a statement about `WorkspaceGenerationStale`.)
- The global handler at `main.py:514` registers `WorkspaceDeleted` only.
- Maintenance paths classify only deletion as benign: `handover.py:82` and
  `permissions.py:79` catch `WorkspaceDeleted` in their skip clauses, and
  `checkpoint.py:219` does the same.

Left unaddressed, a stale generation becomes a **500**, an unnecessary room eviction, or
an **error-level maintenance log** — and the room paths would evict for a condition that
is not an error.

**There are THREE clean-error tuples, not one** — revision 2 named only
`_CLEAN_ACCEPT_ERRORS`. `WorkspaceGenerationStale` must enter all three, or those paths
evict the room before returning their 404, and the eviction happening first is the part a
response-code-only test would not notice.

**Revision 3 then mis-assigned one of them.** It paired annotation-insert with
`_CLEAN_INIT_SPEC_ERRORS`; `insert_annotation_via_kernel` actually catches
`(*_CLEAN_CITE_ERRORS, InsertConflict)` at `collab_cite.py:199`. Only spec-init reads
`_CLEAN_INIT_SPEC_ERRORS`. Coverage happens to survive because the direct-cite row already
adds stale to `_CLEAN_CITE_ERRORS` — but **a test asserting annotation-insert maps via the
init-spec tuple would be asserting something false**, and a reader trusting the old row
could drop the `_CLEAN_CITE_ERRORS` edit and leave annotation-insert evicting the room.

**And the maintenance row was not executable as written.** "Added to the same `isinstance`
tuples" holds for the evict hook and the handover, but the tail checkpoint uses a bare
`except WorkspaceDeleted:` clause — there is no tuple there to extend.

**`F3e1b` ships an explicit per-caller matrix**, and no site converts before its row is
filled in:

| Caller class | Deleted today | Stale must become |
|---|---|---|
| Write routes (save, cite, accept, spec lock/unlock, `save_version`) | 404 via the global handler | **404**, same body — by extending the `main.py:514` handler to both types |
| Collab accept | `_CLEAN_ACCEPT_ERRORS` (`kernel_accept.py:43`) → clean error, no eviction | **clean error, no eviction** — added to that tuple |
| Collab direct cite | `_CLEAN_CITE_ERRORS` (`collab_cite.py:34`) | **clean error, no eviction** — added to that tuple |
| Collab spec init | `_CLEAN_INIT_SPEC_ERRORS` (`collab_cite.py:44`, caught at `:142`) | **clean error, no eviction** — added to that tuple |
| Collab annotation insert | `_CLEAN_CITE_ERRORS` — caught at `collab_cite.py:199` as `(*_CLEAN_CITE_ERRORS, InsertConflict)`, **not** the init-spec tuple | covered by the direct-cite row's edit; asserted separately |
| Maintenance — evict hook, handover | `isinstance(exc, (…, WorkspaceDeleted))` (`permissions.py:79`, `handover.py:82`) | **benign skip** — extend those tuples |
| Maintenance — tail checkpoint | a **bare** `except WorkspaceDeleted:` (`checkpoint.py:219`), no tuple to extend | **benign skip** — widen the clause to `except (WorkspaceDeleted, WorkspaceGenerationStale):` |
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
- **`F3f`** — the pins. Nothing in F3e-1 retires one, and revision 3's reason was wrong.
  **Four** of the five exempt modules name `repo_dir` — `audit.py`, `citations.py`,
  `history.py`, `proposal.py`. **`references.py` does not**; it is held by
  `session_factory`. Both locators retire only with the capability holders, so the pin
  count is unchanged either way — but "all five name `repo_dir`" was a measured fact
  asserted from memory, and it was false.
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
2. A `subprocess` git-commit outside the declared sinks fails the argv guard, AND a new
   process-execution surface (`os.system`, a git library import) fails the
   execution-surface allowlist — both demonstrated.
3. An aliased sink import is still discovered — demonstrated by fixture.
4. A hand-edited `fenced: true` does not survive regeneration — demonstrated.
4b. **A sink with no mutex on its path measures `false`, not `true`** — demonstrated
   against `build_bibliography`, the site revision 2's predicate would have passed.
5. A new git-committing function arrives `UNCLASSIFIED` and the gate refuses it —
   demonstrated.
6. **No production behaviour changes.** Suite green with no site converted — which
   requires the four machinery updates §3.6 and §3.7 enumerate, each of which reddens the
   suite on its own: `test_inventory_merge.py:113`'s `== 20` → `29`;
   `CATEGORIES_BY_KIND[KIND_GIT_COMMIT]` plus an explicit `semantic` category on all nine
   rows; `F3D_IDENTITIES`' `_write_citekey_cache` tuple `"F3e"` → `"F3e2"`; and
   `test_the_ingest_row_is_untouched_and_still_f3e`'s assertion, name and docstring.
7. **`resolve_access` is unchanged** — its six call sites still destructure two values.
7b. **`stamp_for_workspace` exists, refuses a workspace with no generation
   (`UnfencedInvocation`), and has zero production callers** — the carrier ships before the
   conversions that use it, as F3b's ports did.
7c. **The stamp-provenance gate is armed**: a fixture passing an inline
   `session.get(Workspace, wid).lifecycle_generation` to a mechanism-2 acquisition fails
   it, and fails on that named assertion. This is the late-read defect four revisions could
   only warn about.

**`F3e1b`:**

8. `acquire_commit_mutex_at_generation` has production callers for the first time.
9. All seven enclosing acquisitions (§4.2) converted, and reverting any one fails the
   no-`_live`-reaches-a-sink gate on its named assertion — demonstrated.
10. The §5.1 gate's "first effect" boundary is the **`Doc` mutation** (§4.2's table), not
    `append_update`. Demonstrated by a fixture that mutates the room before acquiring and
    is rejected — the case a gate anchored one line late would have accepted.
11. For each entry point in §7.1, a delete+restore between **mint and effect** leaves
    HEAD unchanged, the worktree clean, file contents byte-identical, and **no
    `yjs_updates` row** — with compensation disabled. Driven through the real route or
    orchestrator, never by handing `checkpoint_artifact` a stamp built by the test.
11b. **Every mechanism-2 acquisition's generation comes from a `FenceStamp`** — no site
    reads one inline, enforced by §7.0.3's gate rather than by review.
12. Every §8 mapping asserted by a test, across all three clean-error tuples **and both
    maintenance shapes** (the two `isinstance` tuples and the tail checkpoint's bare
    clause); no path yields a 500, a spurious eviction, or an error-level maintenance log.
13. **Authorization is unchanged.** A collaborator with write access can still cite and
    accept through the collab paths — asserted, because an earlier draft of §7.1 would
    have revoked it.
14. Pins stay at 5, and the slice's own tests say so.
