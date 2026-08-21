# Fence F3e-1 — the mechanism-2 gate for git effects

**Status:** design, approved 2026-08-21. Successor to F3d3b (`eb7d4a34`).
**Governing architecture:** `2026-08-17-lifecycle-fence-design.md` (why a fence exists).
**Governing mechanism spec:** `2026-08-18-fence-session-ownership-design.md` (how an
effect is expressed). This document is a **sub-slice design**: it does not supersede
either, and where it departs from the F3e section of the mechanism spec it says so and
why (§8).

**F3e is split.** This document is **F3e-1**. `2026-08-18` §7 allocated to F3e both the
ingest pipeline and the mechanism-2 effects; that is two subsystems sharing one new
mechanism, and one of them (ingest) carries a design question the spec explicitly
defers. F3e-1 makes mechanism 2 real and proves it on the git-committing citation and
checkpoint paths. **F3e-2** takes the ingest pipeline, the filesystem effects, the
half-ingest stale-generation question, and the `references.py` reads.

---

## 1. The hazard

`acquire_commit_mutex_live` (`drafting/locking.py:257`) takes the exclusive commit
mutex and then re-reads `soft_deleted_at` under it. That defeats *delete*.

It does not defeat **delete-then-restore**. A restore clears `soft_deleted_at` and
leaves `lifecycle_generation` at G+2. An actor that captured G before blocking on the
mutex passes the `_live` check and commits to git against a workspace it no longer
knows anything about. The row-level compensation that masks this for `UPDATE`
statements (§3.5 of the mechanism spec) does not exist for a git commit at all: git has
no target row for PostgreSQL to re-check, and **a database transaction cannot roll back
git**.

`acquire_commit_mutex_at_generation` (`fencing.py:115`, Primitive B) closes exactly
this: mutex, then one scalar `SELECT` of `(soft_deleted_at, lifecycle_generation)`,
raising `WorkspaceDeleted` or `WorkspaceGenerationStale`.

**It was built in F1 and has zero production callers.** Every git-committing site in
the repository today calls `acquire_commit_mutex_live`. F3e-1 is the slice that uses
the primitive for the first time.

## 2. What this slice is not

**No port operation, and no code moves behind a port.** This is a deliberate departure
from a reading of `2026-08-18` §7 that expects F3e to express its effects as port
operations, and the reasoning is in §8.

Ports exist so that **tools** never hold a `Session` (`2026-08-18` §2: "Tools receive
capabilities, never handles"). The git-committing sites are **services** — route
handlers, the collab write kernel, the accept path. A service legitimately holds a
session; handing it a port would not remove a handle it is entitled to. What a service
owes the fence is narrower and entirely mechanical: **it must hold the exclusive mutex
at a known generation before its first effect**. The mechanism spec already frames
mechanism selection as a property of the lock rather than of the port — §3.5's third
mechanism reads "where the caller ALREADY holds the workspace row under FOR UPDATE, **do
not call this at all** — assert the generation on that already-locked row."

So: **ports are for tools; mechanisms are for effects.** A service can be
mechanism-2-correct with no port in sight.

Also out of scope, explicitly:

- **Retires no exemption pin.** Pins stay at **5** (`audit.py`, `citations.py`,
  `history.py`, `proposal.py`, `references.py`). Every one still names `repo_dir`, which
  retires with the capability holders in **F3f**.
- **`_write_citekey_cache` stays `fenced: false`.** Its row rides along with
  `_insert_citation_locked`'s conversion but is not the fenceable unit, and this slice
  does not claim it. See §7.
- **Filesystem effects** (`write_atomic`, `write_text`, `delete_one`) are F3e-2's. This
  slice's new detector kind covers git commits only.
- **Background job families** stay F4's, including `review/pass_runner.py`, which
  git-commits and is the one site with a genuine long capture-to-effect gap (§6).

## 3. Detection widens — repo-wide, following existing precedent

### 3.1 Why repo-wide, and why that is not a new concept

Every previous widening (F2 → F3, and again in F3d3a) kept detection inside the
**effect scope** — `tools/` plus the declared `RELOCATED_EFFECT_PATHS`. That scope is
tool-centric, and git effects live in services. Measured against the current tree, a
scope-limited git detector discovers **exactly one** site
(`citations/core.py:_insert_citation_locked`) — and that one is the *retiring*
flag-off path (§5). It would miss all eight that matter.

Repo-wide discovery is **already how two of the three existing kinds work.** Only
`KIND_TOOL_COMMIT` is guarded by `_in_effect_scope` (`detectors.py:159, 168, 202`).
`KIND_CLAIM_FN` (`:147`) is not, and neither is the `tool_context` scan. The inventory
holds 6 `claim_fn` rows and 5 `tool_context` rows in `analysis/`, `coherence/`,
`phases/`, `review/`, `uncited_claims/` and `drafting/` — all outside `tools/`.

A repo-wide `git_commit` kind is therefore the same shape as `claim_fn`, not an
invention.

### 3.2 The new kind

```
KIND_GIT_COMMIT = "git_commit"
```

A site is any function that calls `write_and_commit_files` **directly**. Discovery is
repo-wide over `src/`, and the row carries the existing shape
`{kind, path, symbol, category, fenced, slice}`.

**Direct callers, not transitive reachers, is the load-bearing choice.** It is what
makes this slice bounded: `checkpoint_artifact` is a single site that ten call sites
funnel through, so converting it covers the four `collab/kernel.py` paths, the evict
hook, the kill-switch handover, `web/collab_checkpoint.py`, and the spec lock/unlock
routes at once. A transitive rule would enumerate all of them and force this slice to
touch every one.

### 3.3 The eight sites and their slice allocation

Measured against the tree at `eb7d4a34`:

Slice names below are the `SLICE_STATUS` keys (§3.5), not the prose slice names.

| Path | Symbol | Slice | Note |
|---|---|---|---|
| `collab/checkpoint.py` | `checkpoint_artifact` | **`F3e1`** | the live prod path; 10 call sites funnel here |
| `citations/core.py` | `_insert_citation_locked` | **`F3e1`** | flag-off citation path |
| `citations/human_cite.py` | `insert_citation_for_user` | **`F3e1`** | flag-off owner select-to-cite |
| `editing/save.py` | `save_section` | **`F3e1`** | same choke-point argument; see §3.4 |
| `analysis/insert.py` | `insert_from_annotation` | `F3e2` | annotation insert |
| `citations/bibliography.py` | `build_bibliography` | `F3e2` | |
| `review/pass_runner.py` | `run_review_pass` | **`F4`** | job family; real capture-to-effect gap (§6) |
| `web/api/workspaces.py` | `post_init_spec._git` | `F3e2` | setup-time |

Four convert in this slice. Four are discovered, recorded with `fenced: false`, and
allocated — which is the point of discovering them now rather than later.

### 3.4 Why `save_section` is in

It is not a citation path. It is in because leaving it out would give the gate an
unexplained hole: it is a direct `write_and_commit_files` caller with the same shape as
the others (`acquire_commit_mutex_live` at `editing/save.py:183`, git commit at `:275`),
and a rule that skips it needs a reason that does not exist. A gate with an unjustified
exemption is the kind of thing this project has repeatedly discovered was hollow.

### 3.5 The `SLICE_STATUS` split and the exact inventory arithmetic

**The gate is bidirectional** (`test_each_slice_matches_its_declared_status`): a
`pending` slice needs **every** row `false`, a `complete` slice needs **every** row
`true`. There is no partial state, which is why F3d needed one key per sub-slice
(`inventory.py`'s own comment: "a multi-slice conversion needs one key per slice or the
first flipped row goes red"). F3e needs the same.

`SLICE_STATUS` keys use no hyphen — `F3d1`, `F3d2`, `F3d3` — so the new keys are
**`F3e1`** and **`F3e2`**, and the existing `"F3e"` key is removed.

The two rows currently allocated to `F3e` — `_write_citekey_cache` and
`fetch_reference_impl._do_fetch_and_ingest` — **both belong to F3e2**. Neither converts
here. `merge()` preserves `prev["slice"]` (`inventory.py:297-300`) because `slice` is a
**human-owned** field, so regeneration will not move them: the reassignment is a
hand-edit of `fence_inventory.json`, proven by showing that a regeneration afterwards is
a no-op. This is the same move F3d3b's Task 1 made, and the same trap: a
`git diff --exit-code` on a file the task deliberately edited can never pass, so the
no-op proof must compare a regenerated copy against the committed file, not ask git
whether the file changed.

Final shape:

| Slice | Rows | Status |
|---|---|---|
| `F3e1` | 4 (all new `git_commit`, all `fenced: true`) | `complete` |
| `F3e2` | 5 (2 inherited from `F3e` + 3 new `git_commit`, all `false`) | `pending` |
| `F4` | 2 (1 existing `claim_fn` + 1 new `git_commit`, both `false`) | `pending` |

Totals: **20 rows → 28**. Kinds: `tool_commit` 9, `claim_fn` 6, `tool_context` 5,
`git_commit` 8.

### 3.6 Discovery is not enough — `fenced` must be MEASURED for the new kind

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
This slice's central claim, "four sites measure fenced", would be a value someone typed.
That is the same family of defect as a test a comment can satisfy, and F3d2 already fixed
it once for `tool_commit` by turning `fenced` into a measurement.

**F3e-1 extends the measurement to `git_commit`, and defines it as the same predicate the
AST gate enforces:**

> a `git_commit` site is `fenced` when it calls `acquire_commit_mutex_at_generation` and
> does **not** call `acquire_commit_mutex_live`.

Deriving the gate and the measurement from **one function** is deliberate. F3d3a found a
hole where discovery and measurement disagreed about what a raw commit *is* — a
`begin`-only effect owned no inventory row at all, so nothing could notice it missing.
Two predicates that must agree will eventually not; one predicate cannot disagree with
itself.

**Two consequences the plan must carry:**

1. **`test_non_tool_commit_rows_keep_their_stored_flag` changes.** It asserts the old
   scoping and will be wrong once `git_commit` is measured. It is narrowed to the kinds
   that genuinely keep a stored flag (`claim_fn`, `tool_context`) rather than deleted —
   the coverage migrates, it does not vanish.
2. **`_SLICE_FOR_KIND` needs an entry, and the default is a trap.**
   `_DEFAULT_SLICE = "F3d2"`, which is **`complete`**. A newly discovered `git_commit`
   site falling through to it would arrive `fenced: false` inside a `complete` slice and
   redden the bidirectional gate for a reason unrelated to the change that introduced it.
   The new kind maps to **`UNCLASSIFIED`**, which `inventory.py` already documents as the
   value "the gate REFUSES … so a regeneration cannot quietly admit an unreviewed effect
   site." A new git-committing function should stop the build until a human allocates it.
   That is the correct behaviour and it must be proven by adding one, not asserted.

## 4. The rule

> **A function that git-commits must have acquired the exclusive mutex at a known
> generation before its first effect of any kind.**

"Before its first effect of any kind" is the mechanism spec's phrasing (§3.5) and it
matters: the mutex is taken before the first *read* that the decision depends on, not
merely before the git call. `checkpoint_artifact`'s existing comment already documents
why — reading the watermark before the mutex lets a racing checkpoint rewrite git HEAD
backwards.

### 4.1 Enforcement — two mechanisms, because one is never enough here

**(a) An AST gate.** For every `git_commit` site: the function calls
`acquire_commit_mutex_at_generation`, does **not** call `acquire_commit_mutex_live`, and
the mutex call lexically precedes the `write_and_commit_files` call.

The lexical-precedence check is weaker than the real requirement (it cannot see a call
inside a helper, and lexical order is not execution order under branching). It is a
cheap ratchet, not the proof. The proof is (b).

**This gate must not be satisfiable by a comment.** The project has shipped that mistake
four times — most recently in F3d3b, where a new test's `or` clause admitted a
substring match against `ast.dump`, and before that in F3d3a, where a drift test
asserted a constant's *name* appeared in `inspect.getsource` and passed under the
mutation because the comment beside the code contained the string. Every assertion here
keys on parsed AST call nodes, never on source text.

**(b) A runtime proof per converted site — the two ordered lock cases**, which
`2026-08-18` §9 ("Every effect operation is proven by TWO ORDERED LOCK CASES") already
mandates for F3b and F3d–F3f:

1. **Case A — effect first, lifecycle second.** The effect takes the mutex at G and
   commits; the delete/restore then blocks and proceeds after. The effect's write
   **lands**, and that is correct: it was live when it ran. Case A is what proves the
   fence does not simply refuse everything.
2. **Case B — lifecycle first, effect second.** Delete+restore commits to G+2, then the
   effect acquires the mutex, evaluates against the new generation, and lands
   **nothing**.

Mechanism 2 takes the exclusive mutex rather than a shared lease, so the deadlock that
killed `2026-08-18` revision 7's straddle test does not apply — but neither does the
straddle: both cases are decided by which side acquires the mutex first, exactly as §9
prescribes.

**Case B needs git-specific assertions, and this is the whole point of the slice.** A
proof that inspects only rows passes while bytes are already on disk — the failure mode
`2026-08-18` §7 names for F3e ("bytes land before rows, so a proof that only inspects
rows passes while a stale generation has already written to disk"). And §9 is explicit
that **an unchanged HEAD is not enough**: a stale implementation can dirty the working
tree without committing. Each Case B test therefore asserts, on the workspace repo:

- `git rev-parse HEAD` is unchanged;
- `git status --porcelain` is empty;
- **the file contents are unchanged** — the bytes of `proposal.md` (and
  `citations.json` where the site touches it) match what they were before the effect
  woke. Cleanliness alone would pass an implementation that wrote and then reverted.

**The compensation must be disabled for each per-site test** (`2026-08-18` §9, per
`2026-08-17` §9). Cancel-on-delete is still present in the tree and masks staleness for
statements that touch the same row; a conversion test that passes with the fence removed
is measuring the compensation, not the fence. This is the technique that exposed Slice
C1's decorative tests, and every Case B test here runs with it off.

### 4.2 A mutation proof for the gate itself

The AST gate is new, so it gets the treatment the project's own review standard now
demands: reverting one converted site to `acquire_commit_mutex_live` must fail the gate,
**and fail it on the named assertion for the named reason** — not by aborting
collection. `tests/fencing` is *not* insulated from the app import chain (the root
`tests/conftest.py:135` autouse teardown imports the app for every test in the repo), so
a broken import shows up as mass teardown **ERRORS with zero FAILED**. Every
stop-the-run judgement in the plan keys on **FAILED**, never on ERROR.

## 5. Which path actually runs, and why it changes the target

Both citation routes fork on `is_collab_enabled` (`web/api/workspaces.py:476-482` and
`:1027-1033`). **Collab is ON in production** (enable-on-touch, shipped in D4
`0c53a72`). So in production today:

- the live citation write path is `collab/kernel.py` → `checkpoint_artifact`;
- `_insert_citation_locked` and `insert_citation_for_user` are the flag-off paths, and
  their own comments describe them as *"retiring rather than maintained"* — with collab
  enabled their severance recheck raises `CollabSeveranceConflict` → 409.

This is why §3.1 chose repo-wide detection. A slice that fenced only the in-effect-scope
site would have fenced retiring code and reported F3e's citation half done while the
path production runs stayed unfenced and, worse, **undiscovered** — the exact
"discovered but never measured, or measured but never discovered" hole that
`detectors.py`'s own docstring warns is "a hole nothing reports".

The flag-off paths still convert. They are reachable (the flag can be off for a
workspace that has never been touched), and leaving two of four sites unconverted would
make the AST gate's exemption list carry entries with no reason.

## 6. Where each site gets its generation

This is the part that is wrong if hand-waved, so it is stated per caller.

`acquire_commit_mutex_at_generation(session, ws_id, generation=G)` requires the caller
to know G. Reading G immediately before the call and asserting it is not a tautology —
under the mutex it pins G for the remainder of the transaction — but it only *adds*
safety where there is a gap between capturing G and landing the effect.

**Route-driven sites** — `save_section`, `insert_citation_for_user`. Both already take
a `Workspace` **instance**, loaded by the route for authorization *before* the mutex is
acquired. That load is the capture point, and the gap between it and the mutex is real:
the handler authorizes, does its request-shaped work, then blocks on a mutex another
writer may hold for seconds. A delete+restore committing in that window leaves
`soft_deleted_at` clear, so `_live` passes. **G comes from the already-loaded instance**
(`workspace.lifecycle_generation`), which is exactly the value the actor believed when it
decided to act.

This is not a formality for these two. It is the same shape as the Slice C1 defect fixed
in `701fbb7`, where `session.get()` returned the identity-map instance loaded *before*
the mutex block and carried the pre-race generation — the primitive's own docstring
records it. Here we *want* the pre-block value: it is the claim being checked. What must
not happen is re-reading G under the mutex, which would make the assertion compare a
value against itself.

**`checkpoint_artifact`** — there **is** a gap. The room operation (the Yjs update, the
serialized markdown) is assembled by the caller before `checkpoint_artifact` is entered.
G is therefore a **parameter**, captured by the caller where the operation is assembled,
and asserted at the mutex. This is a signature change across **10 call sites**, and it is
the largest mechanical piece of the slice.

**Nested acquisition is the subtle case and must be handled explicitly.**
`kernel_accept_prose` takes `acquire_commit_mutex_live` at `collab/kernel.py:208` and
then calls `checkpoint_artifact`, which takes the mutex again at `checkpoint.py:91`. The
existing comment records that this is safe because PostgreSQL advisory xact locks are
re-entrant within a transaction. Once both are `_at_generation`, the **inner call must
receive the same G the outer asserted**. It cannot re-read one: re-reading under a held
mutex would return the same value and so hide a caller that passed a different G. The
generation flows outward-in as a parameter, and a test asserts that a caller passing a
*different* G to the inner call raises rather than silently proceeding.

**`_insert_citation_locked`** — called from `accepting.py` with `owns_txn=False` inside
a transaction whose caller holds the `ProposedChange` row `FOR UPDATE`. That does **not**
qualify for mechanism 3: `2026-08-18` §3.5 restricts mechanism 3 to the **`Workspace`**
row, because "those rows do not carry the workspace's lifecycle state, so locking one
proves nothing about the generation." So it takes mechanism 2 with G supplied by its
caller.

**`ProposedChange` carries no generation column.** The five job tables carry
`claimed_at_generation`; `ProposedChange` does not. This slice does **not** add one. A
proposal created at G and accepted at G+2 after a delete/restore is a real question —
whether accepting it should be refused — but it is a *product* question about proposal
validity, not a mechanism question, and answering it by adding a column here would smuggle
a behaviour change into a fencing slice. Recorded in §9 as deferred.

## 7. `_write_citekey_cache` — carried, not claimed

The row reassigned from F3d3 to F3e in F3d3b stays `fenced: false` at the end of this
slice, and the slice says so rather than letting the reader assume otherwise.

Its `owns_txn=True` branch — the `session.commit()` at `citations/core.py:521` that is
the only reason the site is discovered at all — is reached solely from
`insert_citation_impl`, which **has no production caller**. It was de-allowlisted as a
tool in `855d59ae`, and only six test files drive it.

**Deleting that branch would make the row disappear, and that is precisely what must not
happen.** It would remove a discovered site while the live hazard — a git commit landing
on a stale generation — remained, and `inventory.py`'s own comment on `F3D_IDENTITIES`
forbids it: "Deleting an entry here is not a way to make a test pass. The entry leaves
when the identity is genuinely gone from the design."

The row's honest disposition is: it becomes fenced when the sequence that contains it is
mechanism-2 correct **and** the tool-side measurement (`fenced` = a port write and no raw
commit) can see it. The first half lands here; the second is a measurement question that
belongs with the ingest slice, which faces the same "an effect outside `tools/` needs a
measurement that is not port-shaped" problem. **F3e-2 owns it.**

## 8. Where this departs from `2026-08-18` §7, and why

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
2. **The sequence §7 names is the retiring one** (§5). An operation built around
   `_insert_citation_locked` would not cover the path production runs.

The mechanism-gate approach satisfies what §7 was *for* — the exclusive-mutex mechanism
arriving, and git effects proving they did not land on a stale generation — without
either cost. **This document amends `2026-08-18` §7** accordingly: its F3e section is
split into F3e-1 (this design) and F3e-2, and its expectation that the citekey row's fix
is a port operation is corrected to a mechanism conversion. That amendment is written
into the spec file as part of this slice's first task, so the two documents do not
disagree in the tree.

## 9. Deferred, with owners

- **F3e-2** — the ingest pipeline, filesystem effect detection (`write_atomic`,
  `write_text`, `delete_one`), the half-ingest stale-generation answer, the
  `references.py` reads, `_write_citekey_cache`'s measurement, and the three F3e-2 rows
  from §3.3.
- **F4** — `review/pass_runner.py:run_review_pass`, the one git site with a long
  capture-to-effect gap (it claims at `claimed_at_generation`, runs LLM work, then
  commits). It belongs with the job families.
- **F3f** — the pins. Nothing here retires one.
- **Deferred, no owner yet:** whether accepting a `ProposedChange` created at G should be
  refused after a delete/restore to G+2 (§6). A product question, not a mechanism one.
- **Open, pre-existing, not blocking:** `_safe_tool`'s `ports is None` `RuntimeError`
  (`tools/registry.py:292-301`) raises outside the try/except that keeps exceptions out
  of MCP handlers. Unreachable today — every production caller is wired.

## 10. Testing

Beyond the two ordered lock cases per converted site (§4.1b) and the gate mutation
(§4.2):

- **Behaviour parity is the bar.** The four converted sites must be byte-identical in
  behaviour except for one sanctioned change: a stale generation now raises
  `WorkspaceGenerationStale`, which each caller maps to its existing deleted-workspace
  outcome (404 / `DELETED_ERROR_MESSAGE` / a clean abandon), never to a new one. Anything
  else that has to change to make a test pass is a behaviour change: report it, do not
  absorb it.
- **The nested-generation test** (§6): an inner `checkpoint_artifact` handed a different
  G than the outer acquirer asserted must raise, not proceed.
- **Inventory shape**: rows go 20 → 28 (eight new `git_commit` rows); kinds become
  `tool_commit` 9, `claim_fn` 6, `tool_context` 5, `git_commit` 8. Pins stay 5.
- **Full suite** `uv run pytest -n 16 --dist loadfile -q`; never `-n auto` (resolves to 72
  here) and never `--dist loadgroup`. `ruff check` is a gate; `ruff format` is not.

## 11. Success criteria

1. `acquire_commit_mutex_at_generation` has production callers for the first time.
2. All eight `git_commit` sites are discovered and recorded; four are converted and
   measure fenced; four are allocated to F3e-2/F4 with `fenced: false`.
3. Reverting any converted site to `acquire_commit_mutex_live` fails the AST gate on its
   named assertion — demonstrated, with output, not asserted.
4. For each converted site, a delete+restore between capture and effect leaves **HEAD
   unchanged, the worktree clean, and the file contents byte-identical** — with
   cancel-on-delete compensation disabled, so the test measures the fence and not the
   compensation.
5. A `git_commit` row's `fenced` value is **recomputed on every regeneration**, so a
   hand-edited `true` does not survive. Proven by editing one to `true` and regenerating.
6. A newly introduced git-committing function arrives `UNCLASSIFIED` and **fails** the
   gate until a human allocates it. Proven by adding one, not asserted.
7. Pins stay at 5, and the slice's own tests say so.
