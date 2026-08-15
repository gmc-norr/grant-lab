# Slice C3 — bounding the mutex-held tail at the root

Date: 2026-08-15
Status: approved (design), revision 5

## Revisions

- revision 5 (2026-08-15, found during execution): two corrections, both from things reviewers
  and implementers caught that this spec asserted without checking. (a) 4.2 claimed the
  app-level handler "cannot help: a WebSocket has no HTTP status". **False.** Starlette's
  `_exception_handler.py` never branches on `scope["type"]` -- it builds `conn` as a `Request`
  or a `WebSocket` and runs the registered handler either way, then awaits whatever response it
  returns. The handler DOES run; what it emits on an already-accepted socket is a denial
  response no browser client can see or act on. The conclusion (the route needs its own arm ->
  1013) is unchanged and stronger. (b) The site inventory was built by enumerating **direct**
  `acquire_commit_mutex*` call sites, so it was blind to every site that reaches the mutex
  indirectly. `collab/permissions.py:61` is one -- see 4.3.
- revision 4 (2026-08-15, user review of the plan): five corrections, all found by reading the
  code the plan pointed at. (a) 8's kernel bullet still described the behaviour revision 2
  withdrew. (b) The clean-list gate covered two lists; there are **three** -- it missed
  `_CLEAN_INIT_SPEC_ERRORS` (`collab_cite.py:44`), which guards a path that commits after
  mutating the doc. (c) 7's git timeouts had no exception translation:
  `subprocess.TimeoutExpired` is a sibling of `CalledProcessError`, not a subclass, so adding
  `timeout=` alone would bypass `write_and_commit_files`' entire restore-and-reset rollback and
  leak raw out of the read helpers -- strictly worse than the stall it replaces. (d) 4's
  "Collab, other" row said "verify both" when both in fact need code: a bounded tail raises a
  raw `OperationalError`, which neither `collab_ws.py`'s close-code mapping nor `flip.py`'s
  defer arm catches, and no HTTP handler can reach a WebSocket. (e) 8's background-absorption
  bullet did not say which row to contend on; `acquire_commit_mutex_live`'s liveness read takes
  no row lock, so the obvious choice does not stall a checkpoint at all.
- revision 3 (2026-08-15, plan self-review): the constant was named
  `MUTEX_TAIL_LOCK_TIMEOUT_MS` but used in arithmetic denominated in seconds. Renamed to
  `MUTEX_TAIL_LOCK_TIMEOUT_SECONDS`, matching the ceilings test's units and every sibling
  constant in the tree.
- revision 2 (2026-08-15, found while writing the plan): §4's kernel row instructed the
  implementer to add the tail timeout to the collab kernel's clean-error lists. That would have
  **skipped room eviction after a mutation the transaction then rolled back**, diverging the
  live in-memory doc from the durable log. Withdrawn and replaced by §4.1: the kernel sites need
  no change, because evicting is the correct outcome and is the designed recovery path.
Predecessors: Slice C2 (`eef35ed`), `workspace_write` consolidation (`81ebaec`).
Closes: spec §10 residual 8 of `docs/superpowers/specs/2026-08-11-workspace-retirement-slice-c-design.md`.

## 1. Why

`acquire_commit_mutex` (`src/call_manager/proposal_assist/drafting/locking.py`) sets
`SET LOCAL lock_timeout`, takes `pg_advisory_xact_lock`, translates 55P03 to
`CommitMutexTimeout` — and then, on success, executes at **line 207**:

```python
session.execute(text("SET LOCAL lock_timeout = DEFAULT"))
```

PostgreSQL's default is `0` — **disabled**. So from that statement onward, every lock wait in
the transaction is unbounded, *while the transaction holds the advisory mutex every other
writer for that workspace needs*. One stuck row lock blocks the owner's DELETE and every
concurrent writer indefinitely.

Slice C2 closed this at six route sites by wrapping the remainder in `bounded_lock_waits`; the
consolidation slice moved that ceremony into `workspace_write`. **Twenty-one sites remain.**

A 6-agent inventory of all 21, plus an adversarial refutation pass, established three things
that change the shape of the fix:

**(a) The blast radius is not per-workspace — it is app-wide.** Several of these sites sit in
plain `def` FastAPI routes, which run on the shared anyio threadpool. No `total_tokens`
override exists anywhere in `src/`, so the default of ~40 applies. Forty simultaneous
unbounded stalls wedge **every sync route, for every user, in every workspace**. This is a
global-availability failure mode, and it reverses the intuition that background sites are the
dangerous ones: the HTTP sites are worse.

**(b) Nesting wipes any bound applied from outside.** `checkpoint_artifact`
(`collab/checkpoint.py:90`) re-acquires the same mutex mid-tail — a re-entrant no-op for the
lock, but it re-runs both `SET LOCAL` statements, so it **resets `lock_timeout` to DEFAULT in
the middle of a caller's bounded region**. Ten call sites reach `checkpoint_artifact`. Any
per-caller bound at `kernel.py:136/208/325/369/443` is therefore erased before COMMIT.
`kernel.py:202-204` documents the re-entrancy; nothing documents the reset.

**(c) A third of the sites are dead or retiring.** Three have no production caller at all
(`tools/citations.py:921`, `:1053`, `:1233` — the tool registry at `tools/registry.py:605-608`
exports neither). Five more are flag-off-only paths that answer 409 once collab is enabled.

## 2. Scope

**In:**

- Replace the restore at `locking.py:207` (§3) — this bounds the tail at **all** sites at once.
- One app-level handler mapping the resulting lock timeout to 429 (§5).
- Per-site verification that each of the **13 live** sites' failure path absorbs or maps the
  new raise (§4). This is verification and, where needed, a small local change — not a
  hand-written bound per site.
- Delete the 3 unreachable sites; document the 5 retiring ones (§6).
- Bound the nine `subprocess.run` git calls in `proposal_assist/citations/gitio.py` (§7).

**Out:**

- Any change to `bounded_lock_waits`, `workspace_write`, or the six routes already migrated.
- Any change to the mutex **acquisition** timeouts (25 × 10s, 2 × 5s at `flip.py:32` and
  `hydrate.py:23`). Only the post-acquisition tail changes.
- `subprocess.run` calls outside `gitio.py` (`reconcile.py:54`, `tools/citations.py:152`,
  `tools/history.py:63/98/123`) — none verified to sit inside a mutex-held tail. Residual (§9).

## 3. The mechanism

```python
# locking.py:207
-    session.execute(text("SET LOCAL lock_timeout = DEFAULT"))
+    session.execute(
+        text(f"SET LOCAL lock_timeout = '{MUTEX_TAIL_LOCK_TIMEOUT_SECONDS * 1000}ms'")
+    )
```

The restore is **replaced**, not deleted. Deleting it would leave the tail at the acquisition's
own timeout — 10s at most sites — and §3.1 shows why that fails.

Three properties follow, and they are the whole argument for doing it here rather than at 13
call sites:

1. **It is nesting-proof by construction.** A nested acquire re-applies the same cap instead of
   wiping it. Problem (b) disappears — there is no restore-to-DEFAULT left to run.
2. **It covers sites nobody remembered.** The failure mode this slice exists to fix is a site
   being missed; a root-cause change cannot miss one.
3. **It is one line, not thirteen ceremonies.** The consolidation slice established that a
   ceremony hand-written at N sites is wrong at some of them — it was wrong at four of six.

`MUTEX_TAIL_LOCK_TIMEOUT_SECONDS` is a new named constant in `locking.py`, derived in §3.1.

### 3.1 Why 2 seconds, not 10

`tests/proposal_assist/test_row_lock_ceilings.py` already encodes the arithmetic, and it parses
its inputs out of the code rather than restating them:

- the DELETE route's asserted budget is **6.0s**
  (`tests/proposal_assist/test_phase0_transaction_boundaries.py:1155`);
- `BUDGET_FRACTION_FOR_LOCK_STALLS` = two thirds → lock stalls may consume **4.0s**;
- `MAX_CONTENDED_ROWS` = **2** — a transaction may wait on two distinct rows, and
  `lock_timeout` is a **per-acquisition** cap, not a transaction deadline.

So a mutex-held cap `c` must satisfy `2 × c ≤ 4.0`, giving **c = 2s**. At 10s the worst case is
20s — five times the allowance, and the DELETE budget assertion would fail.

2s also matches every mutex-held cap already in the tree
(`WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS`, `SETUP_JOB_LOCK_TIMEOUT_SECONDS`,
`PASS_PREFIX_LOCK_TIMEOUT_SECONDS`, `MARK_DONE_LOCK_TIMEOUT_SECONDS`, and
`phases/setup.py:136`'s `_ROW_LOCK_TIMEOUT_SECONDS`), so the six `workspace_write` sites keep
setting the same value they already set and nothing conflicts.

`MUTEX_TAIL_LOCK_TIMEOUT_SECONDS` joins `_MUTEX_HELD_CAPS` in the ceilings test, so the arithmetic
stays enforced automatically if the budget or the row count ever moves.

## 4. What the change means per site

The bound applies everywhere at once. What differs per site is what the **raise** does, and the
inventory establishes this per class rather than per site.

| Class | Sites | What changes |
| --- | --- | --- |
| **Background, self-absorbing** | `checkpoint.py:90`, `phases/setup.py:361` | **Nothing.** Both already sit under `except Exception` → rollback + log, with a recovery path: `_fail` (`setup.py:584-641`) requeues to `status='queued'` and dead-letters only at `MAX_SETUP_ATTEMPTS = 5`; `find_checkpoint_candidates` (`store.py:101-127`) re-selects any tail it did not advance. A raise is a retry. |
| **HTTP, already speaks 429** | `web/api/workspaces.py:648`, `:736`, `turns/submit.py:249`, `workspaces.py:124` | The app-level handler (§5) covers them. Verify each; add nothing local. |
| **Collab kernel** | `kernel.py:136`, `:208`, `:325`, `:369`, `:443` | **Nothing** — the existing evict path is the correct outcome. See §4.1. |
| **Collab, other** | `flip.py:60`, `hydrate.py:55` | **Both need code.** Each catches `CommitMutexTimeout` by type; a bounded tail raises a raw `OperationalError`, which walks straight past them. See 4.2. |
| **Dead** | `tools/citations.py:921`, `:1053`, `:1233` | Deleted (§6). |
| **Retiring** | `tools/citations.py:802`, `editing/save.py:180`, `citations/human_cite.py:102`, `analysis/insert.py:351`, `web/api/workspaces.py:537` | Flag-off-only; 409 once collab is on. Documented, not individually handled. |

The 13 live sites are the first four rows: 2 + 4 + 5 + 2.

### 4.1 Why the kernel's clean-error lists must NOT gain the new exception

Revision 1 of this spec said each clean list should gain the tail timeout so it reads as a
clean, retryable refusal. **That is a correctness bug and is withdrawn.**

Those lists mean one specific thing, stated at `kernel_accept.py:36-38`: these errors are
*"raised BEFORE any fragment mutation … Every OTHER exception may have left room.doc mutated
while the txn rolled back → evict to rebuild from the log."*

There are **three** such lists, not two: `_CLEAN_ACCEPT_ERRORS` (`kernel_accept.py:43`),
`_CLEAN_CITE_ERRORS` (`collab_cite.py:34`) and `_CLEAN_INIT_SPEC_ERRORS` (`collab_cite.py:44`).
The third is easy to miss because it is defined ten lines below the second and guards a
different path -- but its `except` at `collab_cite.py:142` wraps a body that commits after
mutating the doc, so it is exactly as exposed as the other two. The gate covers all three.

A tail lock timeout can fire on **either side** of the mutation — at `_load_scoped`'s
`FOR UPDATE` (`kernel.py:210`, `:371`), which is clean, or at `checkpoint_artifact` or the
COMMIT, which is not. One exception type cannot distinguish the two, so listing it as clean
would skip eviction in the case where the room doc **was** mutated and the transaction rolled
back, leaving the in-memory doc diverged from the durable log. Silent divergence in the live
editor is far worse than a disconnect.

So the kernel sites need no change. A tail timeout evicts, and eviction is the designed
recovery, not a failure: `room.py:650-662` closes every client with code 1011 and "the next
connection rebuilds cleanly from the durable log." Measured against today's behaviour — hanging
indefinitely while pinning the mutex for every other writer — a rebuild-from-log blip is
strictly better.

The consequence for scope is that the five kernel sites are **verification only**.

### 4.2 The two "collab, other" sites need code, not verification

Both sit on the far side of the mutex and both catch by exception type, so the bounded tail's
raw `OperationalError` walks past them. Neither is reachable by the app-level handler in the
way that matters.

**`hydrate.py:55`.** It acquires the live mutex and then calls `store.append_update`, which
INSERTs the hydration seed and COMMITs. That INSERT takes an FK parent lock on `workspaces`, so
a concurrent holder of that row now produces a 55P03 from inside hydration. It propagates out
through `MANAGER.acquire`'s `load` callback to the WebSocket route, whose except list at
`collab_ws.py:123-133` is `(WorkspaceRetired, WorkspaceDeleted)` and `CommitMutexTimeout` --
none of which matches. And 5's handler, contrary to what revision 4 of this spec claimed,
DOES run here -- Starlette dispatches registered handlers in the websocket scope exactly as in
the http scope -- but all it can emit on an already-accepted socket is a denial response the
browser client can neither read nor retry on. So the route must answer for itself.
**Decision:** `collab_ws` gains an `OperationalError` arm that maps a lock timeout to the same
1013 (try again later) its `CommitMutexTimeout` arm already uses, and re-raises anything else.
Same animal, same answer.

**`flip.py:60`.** It catches `CommitMutexTimeout` around the acquisition and defers -- returns
`False`, stays legacy, lets the next read retry. But `state.enable_collab` and its
`session.commit()` run AFTER the mutex, and that commit can now raise 55P03. Escaping to the
app handler would turn a routine read into a 429, which is exactly what the defer arm exists to
prevent. **Decision:** `flip` catches it and defers identically.

Both use one shared classifier, `is_lock_timeout(exc)`, added beside the mutex in `locking.py`
so the SQLSTATE check is written once instead of a fourth and fifth time.

### 4.3 The inventory missed every INDIRECT site

The 13-site table above was produced by grepping for `acquire_commit_mutex*` call sites. That
method cannot see a caller that reaches the mutex through something else, and at least one does:
`make_checkpoint_on_evict` (`collab/permissions.py:61`) calls `checkpoint_artifact`, which takes
the mutex.

Its benign arm catches `(GitCommitError, CommitMutexTimeout, WorkspaceDeleted)` by type and logs
a `collab.checkpoint_on_evict_skipped` WARNING. A bounded tail's raw `OperationalError` falls
past it to `except Exception`, which logs at ERROR **with a traceback** -- for ordinary row
contention during room eviction. Nothing corrupts (both arms roll back and swallow), but routine
contention becomes error-level noise in a log this team actually reads.

A sweep of all 23 `except CommitMutexTimeout` sites in `src/` confirms this is the only
uncovered one: the HTTP routes are all answered by 5's handler, three of the
`tools/citations.py` sites are deleted in 6, its fourth is retiring, and `flip`/`collab_ws`
are handled in 4.2. **Decision:** merge the two `except` clauses into one that chooses WARNING
vs ERROR via `is_lock_timeout`, rather than adding an `except OperationalError` arm -- a
`raise` from a sibling clause escapes the `try` entirely instead of falling through, which would
change what a real database failure does here. `acquire_commit_mutex`
itself is rewired onto it, so there is exactly one definition of "this is a lock timeout" in the
tree.

## 5. The app-level handler

`main.py` already registers handlers for `WorkspaceDeleted` → 404 (`:501`) and
`WorkspaceRetired` → 404 (`:512`) — exceptions from this same mutex family, raised at these
same sites. A handler for the tail timeout is a **new member of an existing pattern**, not a new
architecture. (An earlier draft of the Slice C residual claimed the absence of app-level
mapping was deliberate; that was wrong.)

It maps the tail's 55P03 to **429 + `Retry-After: 2` + `BUSY_ERROR_MESSAGE`**, matching what the
six migrated routes already answer.

**Why central rather than per-route.** A local `except` only catches what its `try` encloses,
and the inventory found two sites whose `session.commit()` already sits **outside** the relevant
try block — `turns.py:197` and `proposals.py:145`. A per-route clause would silently miss
commit-time timeouts there. That is precisely the defect Slice C2 spent four review rounds
chasing, pre-existing at two sites. A handler at the app level is immune to where the timeout
fires.

**One deliberate exception.** `web/collab_checkpoint.py:57` answers **503**, not 429, for
`CommitMutexTimeout`. It keeps its local catch and must be verified to still win over the
app-level handler.

## 6. Dead and retiring code

`tools/citations.py:921` (`_remove_citation_locked`), `:1053` (`_update_citation_locked`) and
`:1233` (`_rename_citekey_locked`) have **no production caller**: `tools/registry.py:605-608`
imports only `list_citations_impl` and `propose_citation_impl`, and a tree-wide grep finds no
other entry point. Their own test file's docstring calls them MCP tools, which is stale.

They are deleted, with their tests. Bounding code nothing reaches is waste, and leaving it
implies a liveness it does not have.

Verified independently of the inventory, because this is the one part of the slice that removes
code: `registry.py:605-608` imports exactly `list_citations_impl` and `propose_citation_impl`
and nothing else; a tree-wide grep for `remove_citation_impl`, `update_citation_impl`,
`rename_citekey_impl` and their three `_*_locked` helpers returns **zero** hits outside
`tools/citations.py` itself. The only other references are three test files, which go with them:
`tests/proposal_assist/test_tools_citations_deleted.py`,
`tests/proposal_assist/tools/test_citations_remove_update.py`,
`tests/proposal_assist/tools/test_citations_rename.py`. The first of those is named for a
deletion that was evidently started and never finished.

The five retiring sites stay as they are and gain one comment each recording that they are
flag-off-only and that the tail bound now applies to them regardless.

## 7. Git subprocess timeouts

`proposal_assist/citations/gitio.py` makes **nine** `subprocess.run` calls (lines 87, 96, 103,
113, 120, 148, 178, 198, 220) and passes `timeout=` to **none** of them. `write_and_commit_files`
runs inside `checkpoint_artifact`, i.e. inside a mutex-held tail. A stalled filesystem therefore
stalls a mutex-held transaction with no ceiling, and no lock bound touches that.

Without this, the slice's headline claim — that a mutex-held tail cannot stall indefinitely — is
still false.

Precedent exists in-tree: `phases/setup.py:108` defines `_GIT_TIMEOUT_SECONDS = 30.0` and passes
it. `gitio.py` gets the same treatment with its own constant. 30s is far above the 2s lock cap
because a git timeout is a genuine filesystem failure, not routine contention — it should be
rare and loud, not a routine refusal.

### 7.1 `timeout=` alone would make this worse

`subprocess.TimeoutExpired` is a **sibling** of `CalledProcessError` under `SubprocessError`,
not a subclass. Adding the keyword without handling the exception therefore breaks three
contracts:

- `write_and_commit_files` writes the working tree first, then runs `git add` (`:87`) and
  `git commit` (`:113`). Its rollback lives in `except subprocess.CalledProcessError` at `:130`
  and performs the per-file restore plus a scoped `git reset`. A `TimeoutExpired` on either
  call would skip that block entirely, leaving the repo with modified files and a dirty index
  while the DB transaction rolls back. That is a worse outcome than the unbounded stall.
- The scoped `git reset` inside the rollback (`:148`) acquires a timeout of its own. If it
  raises out of the handler it masks the original error and still leaves the tree dirty.
- `current_head` (`:178`), `blob_sha` (`:198`) and `read_blob` (`:220`) catch only `OSError`
  and document that they raise `GitCommitError`. A raw `TimeoutExpired` breaks that promise for
  every caller.

**Decision:** `write_and_commit_files` catches `(CalledProcessError, TimeoutExpired)` on one
arm and formats both into the same `GitCommitError`; the rollback's `git reset` is wrapped so a
timeout there is suppressed rather than masking; and the three read helpers translate
`TimeoutExpired` to `GitCommitError` alongside `OSError`. Tests inject `TimeoutExpired`
deliberately -- no test waits 30 seconds.

## 8. Testing

1. **The ceilings test extends automatically.** `MUTEX_TAIL_LOCK_TIMEOUT_SECONDS` joins
   `_MUTEX_HELD_CAPS`; the existing assertion `MAX_CONTENDED_ROWS × cap ≤ allowance` then covers
   it, with the budget still parsed out of the DELETE test rather than restated.
2. **The tail is actually bounded.** Park a holder on a row a mutex-held transaction needs, with
   no mutex of its own; the request must come back 429 rather than block. Asserted as "the
   request returned", never as elapsed time — without the bound it does not return at all, so a
   live thread IS the failure.
3. **Nesting no longer wipes the bound.** The decisive gate. Drive a path that reaches
   `checkpoint_artifact` from a caller that already holds the mutex, park a holder on a row the
   checkpoint needs, and assert 429. Mutation: restore `SET LOCAL lock_timeout = DEFAULT` at
   `locking.py:207` and this test must fail — that mutation is the whole slice, and if it does
   not turn something red the slice is unproven.
4. **Background absorption is real, not assumed.** For `setup.py:361` and `checkpoint.py:90`,
   assert the job is **requeued or re-selected** after a bounded failure — not merely that the
   exception was logged. A reaper that filters on a status the failure path never sets recovers
   nothing; this project has shipped that error twice in prose about these exact functions.
   The contended row must be one the path actually locks: `acquire_commit_mutex_live` takes the
   advisory lock and then reads `soft_deleted_at` with a **plain** `SELECT`, so holding the
   workspace row does not stall a checkpoint at all. Contend on what the path writes -- for
   `checkpoint_artifact` that is the `collab_artifact_state` row `advance_checkpoint` updates.
5. **The kernel's clean-error lists stay closed to the tail timeout.** Assert structurally
   that none of the three lists in 4.1 admits a lock timeout. Per 4.1 the kernel MUST evict:
   the timeout can fire on either side of the fragment mutation, so classifying it as "raised
   before any mutation" would skip eviction after a real one and leave the live doc diverged
   from the durable log. (Revision 1 of this spec asserted the opposite here; withdrawn.)
6. **The 503 holdout still wins.** `collab_checkpoint` must still answer 503, not the handler's
   429. The test must enable collab on the artifact first -- the route screens for that and
   returns 409 before it ever reaches the mutex.
7. **The two `4.2` dispositions are proven, not assumed.** Contend the workspace row and assert
   the WebSocket closes 1013 (not 1011), and that `maybe_auto_enable` returns `False` rather
   than raising. Both contend via the FK parent lock their INSERT takes.
8. **Git timeouts translate.** Inject `subprocess.TimeoutExpired` and assert `GitCommitError`
   out of each helper, plus a clean working tree and index after a timed-out commit.
7. Existing gates unchanged and green: the DELETE budget assertion, the two `_m1` gates in
   `test_phase0_transaction_boundaries.py:353` and `:398`, the 28 straddle tests, and the AST
   gate.

Full suite baseline on `master` (`81ebaec`): **3391 passed, 1 skipped**.

## 9. Residuals

- `subprocess.run` in `reconcile.py:54`, `tools/citations.py:152`, `tools/history.py:63/98/123`
  carry no timeout. None is verified to sit inside a mutex-held tail, so they are out of scope —
  but they are unbounded subprocesses in a web process, which is its own (smaller) hazard.
- A lock-order inversion exists between the two accept paths: `accepting.py:74` takes the
  proposed-change row `FOR UPDATE` and *then* the mutex, while `kernel.py:210/371` take the
  mutex then the row. The arms are normally mutually exclusive, but `proposals.py:157-168` reads
  the flag in a separate session before forking, so enable-on-touch can interleave them. The
  outcome is a 40P01 deadlock, which Postgres resolves — not an unbounded wait — so it is out of
  scope here, but it contradicts `kernel.py:200-201`'s claim to pin one order everywhere.
- The anyio threadpool has no configured size anywhere in `src/`, so the ~40 default governs the
  app-wide blast radius described in §1(a). Bounding the tails removes the unbounded case; it
  does not make the pool size a deliberate decision.

## 10. Success criteria

1. `locking.py:207` sets a bounded tail cap; no `SET LOCAL lock_timeout = DEFAULT` remains in
   the tree.
2. Restoring that statement turns at least one test red, and that test exercises the **nested**
   case (§8.3).
3. The ceilings test covers the new constant, with the budget still parsed from the DELETE test.
4. Each of the 13 live sites has a verified disposition: absorbed, mapped by the handler, or
   explicitly handled. No site is left to the catch-all 500 handler.
5. The three dead sites and their tests are gone.
6. `gitio.py`'s nine subprocess calls carry a timeout.
7. Full suite ≥ 3391 passed with nothing removed; `ruff` clean; OpenAPI drift clean.
