# Delete as Quiesce-and-Seal

**Status: REJECTED 2026-08-18**, the day it was written. The safety theorem in §2.3
is sound and every premise it rests on is false in this execution model. Kept for
the record, and because §5 and §9 hold independently.

**Why it fails** — each verified against the tree:

1. **"Confirmed death" is not achievable.** `tools/references.py:527` moves
   ingestion into a worker thread via `run_in_threadpool`, which commits at `:580`.
   **A Python thread cannot be force-terminated.** Waiting for it makes delete
   unbounded; not waiting makes the seal non-final. Current confirmation
   (`claude_adapter.py:635`) waits only on the SDK child.
2. **"The mutex is the drain" is false.** `review/pass_runner.py:344` writes git
   with zero mutex acquisitions.
3. **"The bound is milliseconds" is false.** `citations/gitio.py:21` documents its
   own worst case: *"gitio can hold the commit mutex for ~180s."* Initial
   acquisition defaults to 10s (`drafting/locking.py:205`), not the 2s C3 cap,
   which is only installed *after* acquisition.
4. **"F2 enumerates actors" is false.** The detector matches two function-name
   prefixes. The interactive turn claim is an inline UPDATE
   (`turns/processor.py:215`), collab rooms claim no job, and worker threads claim
   nothing. The proposed registry gate would have been green with real actors
   absent — decorative, in the precise sense this project uses the word.
5. **"The seal captures what was committed" is false.** A git commit does not
   capture `_propose_artifact_edit_impl`'s DB-only `ProposedChange`
   (`tools/proposal.py:159`) or ingest's rows and blob storage.
6. **Admit-stop must commit to be observable** (`workspaces.py:228` already does),
   so a failure after it leaves the workspace deleted with the sequence
   half-run — which needs a durable `active → quiescing → sealed` state machine and
   an idempotent completer. That is the reconciliation machinery §6 claimed to
   delete.

**The general form:** kill-as-proof requires every actor and all nested work to run
in supervised, force-terminable processes. This application is coroutines,
scheduler threads, worker threads, blocking DB calls and child git processes.

**Superseded by** `2026-08-18-fence-session-ownership-design.md` (revision 5).
Quiescing survives as an **operational optimisation** — stop admissions, cancel
promptly, mark where possible — never as the correctness mechanism.

---

**Original status:** proposed 2026-08-18. Alternative to the epoch-fence sequence
for the delete / disable / restore problem.

**Supersedes, if adopted:** `2026-08-18-fence-session-ownership-design.md` (the
ports design) and sub-slices F3a–F3f. It does **not** supersede
`2026-08-17-lifecycle-fence-design.md`'s problem statement, which remains the
correct diagnosis.

---

## 1. The premise that changes everything

The fence exists to make a stale actor's write harmless. It has to, because the
current delete path is a **polite** kill: it asks every actor to stop, waits six
seconds, and then proceeds regardless.

```python
fut.result(timeout=_RETIREMENT_BARRIER_SECONDS)   # 6.0
except concurrent.futures.TimeoutError:
    logger.warning("retirement.barrier_timeout")
    # retirement continues on the loop; degrade to eventual
```

So we already pay for stopping everything, and still do not get the guarantee.
Every fence, barrier, `cancelled_from` and reconciliation pass is compensation for
that gap.

**The premise: the owner pressed delete before the work was done, and accepts that
the last result does not arrive.** Once that is allowed, the actor does not need to
be made harmless — it needs to be dead. A dead actor writes nothing, and no
generation check is required to prove it.

## 2. The sequence

Delete becomes five ordered steps inside one operation.

| # | Step | Bound |
|---|---|---|
| 1 | **Admit-stop.** Stamp `soft_deleted_at`. Every entry point that would *start* work already reads liveness and refuses. | one write |
| 2 | **Drain.** Acquire the workspace commit mutex. | `lock_timeout`, 2s cap (Slice C3) |
| 3 | **Kill.** Terminate every actor and wait for confirmed death. | process teardown |
| 4 | **Seal.** Commit the workspace's current state to git as a final, marked commit. | one git commit |
| 5 | **Release** the mutex. | — |

### 2.1 Why acquiring the mutex is the drain

Every critical section in the codebase holds the workspace commit mutex — 17 direct
call sites plus the `workspace_write()` wrapper. **Holding it means no critical
section is in flight**, because the one that was would have to release it first.

There is no need for a separate barrier that waits on hooks and gives up. The
drain is a lock acquisition, and Slice C3 (`df437b4`) already caps its tail at two
seconds at every site.

**Verified:** no critical section reaches the network. The largest —
`tools/citations.py:792` — acquires the mutex, reads `proposal.md` and the ledger,
calls `write_and_commit_files`, and writes rows. All local I/O. The citation LLM
service is invoked *outside* the mutex; embedding in `tools/references.py` holds no
mutex at all; `references/ingest.py` runs its slow phases on fresh sessions by
design. So step 2's bound is real and is milliseconds in practice.

### 2.2 Why kill precedes seal

An actor that writes **without** taking the mutex — `_propose_artifact_edit_impl`
opens its own session and commits, which is what F2 found — is not drained by step
2. It is handled by ordering: it is killed in step 3, before the seal in step 4.

Anything it committed before dying happened before the seal, so the seal captures
it. It cannot commit after dying. Step 3 waits for **confirmed** death, so a
`COMMIT` already in flight to Postgres has resolved — committed or rolled back with
the dropped connection — before step 4 runs.

### 2.3 The safety argument, stated plainly

After step 3, for any actor A:

- A cannot have *started* after step 1 — entry points refuse.
- A cannot be *inside* a critical section — step 2 holds the mutex it would need.
- A cannot write *after* step 3 — it is dead.

Therefore the state sealed in step 4 is final, and no write can follow it. This is
the guarantee the six-second barrier does not provide, and it costs one lock
acquisition instead.

## 3. What is deliberately lost

**The in-flight LLM result.** Killed mid-call. The owner accepted this by pressing
delete.

**Exact token accounting.** The adapter already keeps a running tally for this
case:

> *"Defensive AssistantMessage capture: holds a running tally so we have some count
> if the ResultMessage never arrives (network drop, early disconnect)."*

That tally lives in the **parent** process, not the killed subprocess, so it
survives the kill and is recorded in step 3. But it is a **floor, not a figure**:
`output_tokens` lags the first delta, and `input_tokens` excludes
`cache_creation_input_tokens`, which the code notes *"dominates for Claude Code
system prompts."* We under-bill ourselves on killed calls. Accepted, and logged as
approximate so the number is never mistaken for exact.

**Nothing else.** Committed work is committed and is captured by the seal.

## 4. Coverage: knowing every actor

The one real risk is forgetting an actor. Today the list is maintained by hand:

```python
REQUIRED_HOOKS = ("turns", "collab", "jobs", "passes")
```

**This is already checkable mechanically, by an artifact on master.** F2's detector
(`9dc820d`) enumerates every actor entry point by AST:

```python
_CLAIM_PREFIXES = ("claim_next_", "claim_queued_")
```

It finds six today, and `test_the_inventory_matches_the_tree_exactly` fails CI on
any new one that is not classified. An actor cannot exist without claiming work, so
it cannot exist without appearing here.

**Two changes make this the kill-list's guarantee:**

1. Widen the prefix rule, or key on the claim's *shape* rather than its name, so a
   future `acquire_next_job` cannot slip past. One line in an existing detector.
2. Add a gate assertion that every `claim_fn` identity has a corresponding entry in
   the kill registry — so adding an actor without making it killable fails CI.

The F2 inventory is thereby repurposed, not discarded: it stops being a *fence*
checklist and becomes an *actor* registry, which is what its `claim_fn` kind
already is.

## 5. Torn state is repaired on restore, not prevented on delete

A hard kill can leave a job row in `running`, or a git repo with a lock file. Under
the fence design this had to be *prevented*, because the actor was expected to
write its own terminal state.

Here it is **repaired on the way back in.** Restore already exists (Slice C1,
`fcedf81`) and already bumps `lifecycle_generation`. It gains a repair pass:
terminalise any row left `running`, clear stale git lock files, and record what it
repaired.

This is strictly easier than prevention: repair runs when nothing is concurrent,
with no deadline, and can be tested directly by killing mid-flight and restoring.

## 6. What this deletes

If adopted, the following become unnecessary:

- `_RETIREMENT_BARRIER_SECONDS` and `_RESTORE_BARRIER_SECONDS`, and the
  `run_coroutine_threadsafe` + timeout dance around them
- the `REQUIRED_HOOKS` fan-out as a *correctness* mechanism (it becomes the kill
  registry, generated and checked)
- `reconcile_cancelled_jobs` and `cancelled_from`, replaced by restore-side repair
- **the entire F3a–F3f sequence** — no ports layer, no DTOs, no import boundary,
  and `tools/` keeps using ORM objects directly
- `503-after-commit` and the delete-path retry semantics that exist because the
  barrier may have given up

## 7. What is kept

- **F1's primitives and `claimed_at_generation`** (`7de5342`). Not load-bearing for
  delete any more, but the column and `live_at_generation` remain the cheapest way
  to make any *individual* write conditional, and F1 is already merged and inert.
- **The F2 inventory** (`9dc820d`), repurposed as the actor registry (§4).
- **Slice C2/C3's mutex discipline** — `workspace_write()` and the 2s
  `lock_timeout` cap are what make step 2's bound real.
- **The problem statement** in `2026-08-17`. Its diagnosis was right; only its
  remedy changes.

## 8. Risks

- **Critical sections must stay short.** Step 2's bound is only as good as the
  longest mutex-held section. Verified clean today (§2.1), but a future section
  that calls an LLM under the mutex silently turns a 2-second delete into a
  two-minute one. This needs its own gate: no network client reachable from a
  mutex-held scope.
- **Kill must be confirmed, not requested.** Step 3 waiting on a signal rather than
  on process death reintroduces the six-second problem in new clothes.
- **The kill registry must be generated, never hand-written.** A hand-maintained
  list is what this design replaces; if the registry is typed by a human it has the
  same failure mode with more ceremony.
- **Disable is not delete.** Disable is meant to be reversible and low-drama.
  Killing in-flight work on *disable* may be too aggressive — this design must say
  explicitly whether disable quiesces or merely blocks new admissions.
- **Under-billing is real money.** The token floor understates spend, and nothing
  reconciles it against the provider's own accounting. Acceptable only because
  killed turns are rare; worth a metric so it stays rare.

## 9. Open question this design must answer before planning

**Does `disable` use the same sequence as `delete`?** Delete has the owner's
explicit acceptance that results are lost. Disable does not obviously carry the same
licence — a user disabling a workspace may reasonably expect in-flight work to
finish or resume. Three coherent answers exist (quiesce like delete; block
admissions only and let work finish; quiesce but re-queue), and they differ in
user-visible behaviour rather than in mechanism. This is a product decision, not an
engineering one, and it is the first thing to settle.

## 10. Slices, if adopted

1. **Q1 — the actor registry.** Widen F2's claim detection, generate the kill
   registry from it, and gate that every actor is killable. No behaviour change.
2. **Q2 — the mutex-drain gate.** Assert no network client is reachable from a
   mutex-held scope, so step 2's bound stays true.
3. **Q3 — confirmed kill.** Replace the polite hooks with terminate-and-wait, and
   record the token floor.
4. **Q4 — the seal.** Final marked commit under the mutex, and the delete route's
   new ordering.
5. **Q5 — restore-side repair.** Terminalise stranded rows, clear git locks, report
   what was repaired.
6. **Q6 — remove the compensation.** Delete the barriers,
   `reconcile_cancelled_jobs` and `cancelled_from`.
