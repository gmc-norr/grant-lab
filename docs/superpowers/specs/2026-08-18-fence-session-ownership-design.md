# Fence F3 — Session Ownership Design

**Status:** agreed 2026-08-18. Mechanism spec for slices F3 and F3b.

**Companion:** `2026-08-17-lifecycle-fence-design.md` is the architecture — why a
fence exists, what the invariant is, and the F1–F9 slice breakdown. This document
is the mechanism: how a fence is *expressed* and how coverage is *enforced*. It
supersedes that spec's §3.4 containment approach and closes three of the four
gaps its §9.1 records.

---

## 1. The problem this fixes

F2 (`9dc820d`) shipped an AST gate that asks **"is every write inside a fence?"**
An external review found five reachable bypasses, each reproduced against the
merged verifier. The root cause was not a set of bugs:

> The detector emits a site only where it sees a literal `.commit()` under
> `tools/`. The canonical form *removes* that commit — `fenced_write` owns it and
> `fenced_execute` needs none. So a converted function stops being detectable and
> newly written canonical-form code is never detected at all. **A gate that gets
> greener the more converted the codebase is inverts its own purpose.**

Writes are an **open set**. To be sound, an enumeration of them must be complete,
and it cannot be: `session.execute(stmt)` where `stmt` was built earlier, `flush`,
`add_all`, `delete`, `merge`, bulk operations, `engine.begin()`, raw connections,
git and filesystem helpers, and any effectful helper in another module all evade
it. Widening the enumeration is the move `2026-08-17` §3.4 forbids — *narrow the
accepted syntax, never widen the analysis.*

## 2. The principle

**Do not enumerate misuse of a resource. Make the resource unobtainable.**

A writable session is obtainable only from inside a fence. Everything done with
it is therefore inside the fence by construction. There is no effect model, so
there is nothing for an effect model to miss.

This is `web/_writes.py::workspace_write()` carried to its conclusion — that
helper "exists so the rule cannot be written wrongly." The same argument applies
one level down, to the handle itself.

## 3. The three forms

`src/call_manager/proposal_assist/fencing.py` gains the four forms below. They
**acquire** the session rather than wrapping one the caller already holds. F1's
`live_at_generation` and `acquire_commit_mutex_at_generation` remain as the
primitives underneath, unchanged.

```python
# Primitive B — mutex + generation. Git, filesystem, or multiple rows that
# must move together. The fence identity comes from ctx; there is no
# caller-supplied generation to get wrong.
with fenced_write(ctx) as w:
    w.add(ProposedChange(...))          # commits on clean exit
    w.execute(update(Artifact)...)      # no commit(), rollback() or begin()

# Primitive A — ONE conditional statement, DB-only. No lock, so no lock-order
# risk. Applies live_at_generation INSIDE the statement.
fenced_execute(ctx, update(Artifact).where(...).values(...))

# Accounting — ONE conditional statement that is REQUIRED to land on a stale
# generation, and so carries NO generation predicate. It must instead bind to
# the identity that originated it (§3.1).
accounting_execute(ctx, update(TurnJob).where(
    TurnJob.id == ctx.origin_job_id, TurnJob.status == "running",
    TurnJob.fetch_count < 3).values(fetch_count=TurnJob.fetch_count + 1))

# Reads — a read-only capability. No transaction-boundary methods, so the
# READ ONLY transaction it opens is the only one it can ever have.
with ctx.read_session() as r:
    row = r.get(Workspace, ws)
```

### 3.1 Why accounting is a STATEMENT, not a session

`2026-08-17` §2 permits and §7 *requires* accounting writes to land on a stale
generation: freeing an active-unique slot and refunding an admission slot must
happen even when the workspace has moved on. Fencing them would strand jobs and
destroy the deliberate late token accounting at `review/pass_runner.py:164`.

But "exempt from the generation check" is not "exempt from atomicity." The
architecture requires stale cleanup via **conditional writes only**, and a general
writable session cannot enforce that — it would happily accept
`job.fetch_count += 1`, a non-atomic read-modify-write. That defect has shipped
here twice (`28abbc4`, `701fbb7`) and is Global Constraint 3 of the F1 plan. So
accounting gets a **statement** form, not a session form.

**The predicate must bind to the originating identity, not to "whichever job is
running."** Today `tools/references.py:463` and `:499` target
`TurnJob.workspace_id == ctx.workspace_id AND status == "running"`. That is atomic
but not owned: after delete → restore → a new turn starts, an actor from the old
generation reserves or refunds **the new turn's** slot. Since it carries no
generation predicate by design, ownership is the only thing that can bound it. F3
narrows both statements to the originating job identity.

**Two-way pin.** The gate requires a site using `accounting_execute` to be
classified `accounting` in the inventory, and every `accounting` site to use it.
Neither direction is optional, so the exemption cannot spread by imitation.

### 3.2 The yielded object is a capability, not a `Session`

`fenced_write` and `read_session` yield a **restricted proxy** that forwards
`add`, `add_all`, `execute`, `get`, `flush`, `delete`, `merge` and query methods,
and does **not** expose `commit`, `rollback`, `begin`, `close`, or the underlying
`Session`. The context manager owns the transaction boundary; the body cannot end
it.

Without this the whole design leaks, in two ways:

- **`fenced_write` would release its own mutex.** The advisory lock is
  transaction-scoped. A helper that commits the session it was handed ends the
  transaction, drops the lock, and any further work autobegins a **new, unfenced**
  transaction inside a Python block that still looks fenced. This is not
  hypothetical: `tools/citations.py:744` takes `owns_txn: bool` precisely because
  committing a received session is an established pattern here.
- **`read_session` would stop being read-only.** `SET TRANSACTION READ ONLY`
  applies to the current transaction only. A `commit()` inside the block ends it,
  and the next statement autobegins an ordinary read-write transaction. Read-only
  must hold for the capability's whole lifetime, which it does when the lifetime
  is exactly one transaction the body cannot end.

So a writable capability is obtainable from `fenced_write` alone, it lasts exactly
one transaction, and that transaction is the fenced one.

### 3.3 The fence identity comes from `ctx`, never from the caller

All four forms take `ctx` and derive `workspace_id` and the generation from it.
None accepts a caller-supplied generation.

A wrapper that accepted one would let a stale actor write
`fenced_write(ctx, ws, generation=freshly_read_generation)` — canonical shape,
passes any containment gate, and fences against the world's current state instead
of against the belief the actor formed. That is the same failure this architecture
names for the retained `ToolContext`: **a generation obtained at the moment of
writing is a stale belief wearing the fence's uniform.** An API that accepts one
as a parameter invites it.

The generation therefore lives on `ToolContext`, stamped per turn, and the fence
reads it. Passing one explicitly is not merely discouraged — the parameter does
not exist.

`fenced_execute` keeps Primitive A's defining property: it takes **no lock**.
Where a finalizer already holds the job or workspace row `FOR UPDATE`, forcing it
through the mutex would create the J→M and W→M inversions `2026-08-17` §5
documents. This is why there are two forms and not one.

## 4. Making the handle unobtainable

**`ToolContext` stores the factory privately.** The constructor keyword stays
`session_factory=` — via `InitVar` / a custom `__init__` that assigns
`self._session_factory` — so all 7 source constructions and all 52 test
constructions across 31 files are untouched. But `ctx.session_factory` raises
`AttributeError`.

This reverses an earlier decision in this design, and the reversal matters. The
earlier version enforced privacy with an AST rule forbidding the attribute read,
on the stated grounds that a rename would cost ~60 files. That reasoning was
wrong twice over. An AST rule does not make a resource unavailable —
`getattr(ctx, "session_factory")` walks straight past it, and `getattr(ctx, ...)`
is already how this codebase reaches optional context capabilities
(`tools/references.py:529`). And the choice was never rename-or-gate: `InitVar`
buys runtime enforcement at zero churn.

**The gate remains, demoted to a second line.** No module under `tools/` may
import a `sessionmaker` or `Engine`. The interpreter enforces the first line; the
gate catches the way around it.

### 4.1 What the closed set actually contains — and what it does not

The claim "a writable session is obtainable only from a fence" is true **for
sessions a tool acquires directly.** It is not true of the whole subsystem today,
and the spec states the boundary rather than overreaching:

- **Sessions injected by callers outside `tools/`.** `proposals/accepting.py:207`
  passes its own request-owned session into `_insert_citation_locked`. So a helper
  under `tools/` that *receives* a session has **not** necessarily received a
  fenced one, and §5's earlier claim to the contrary was false in the live tree.
  Such helpers keep an inventory entry, classified by what their callers can be,
  and `caller_fenced` is retained for exactly this — but rewritten to require the
  fence to enclose the ancestor's whole critical section (`2026-08-17` §9.1 gap 4).
- **Service-owned factories.** `ctx.citation_service` holds its own
  `_session_factory` and commits usage records (`citations/service.py:114`).
  `tools/audit.py:90` passes `ctx.session_factory` into the semantic audit engine,
  which reaches `citations/verdict_cache.py::store_verdict` — a durable cache
  write on a fresh session.

These are **capability holders**: objects that legitimately own a write path.
Each gets a classified inventory entry and its own slice. They are not covered by
F3 and F3 does not claim they are.

### 4.2 Staging: how an absolute rule ships with known violations present

The rule of §4 is absolute in its final state, but F3 lands with `ingest_bytes`
and the audit path still holding raw factories. The two are reconciled by the
inventory, not by weakening the rule:

- A raw acquisition is legal **only** if it is an inventory entry whose slice is
  still `pending` in `SLICE_STATUS`. Those identities are pinned, and there are
  exactly three at F3: `references.py::_do_fetch_and_ingest` (F3b),
  `audit.py::validate_citations_impl` (F3c), and the `citation_service`
  capability (F3c).
- **A raw acquisition that is not a pinned pending identity fails immediately** —
  it arrives as a new site, unclassified, and the gate refuses unclassified sites.
- When a slice flips to `complete`, its identities lose the exemption, and
  `SLICE_STATUS` already fails any row whose flag disagrees.

So the exemption is enumerated, expiring, and cannot be taken by new code.

## 5. What dissolves, and what does not

| `2026-08-17` §9.1 gap | Fate under session ownership |
|---|---|
| **1.** Detector blind to the canonical form | **Dissolved.** The detector looks for *acquisitions*. The canonical form **is** an acquisition, so converted code is *more* visible, not less. The perverse incentive inverts. |
| **2.** `_is_effect` enumerates an open set | **Dissolved.** There is no effect model to be incomplete. |
| **4.** `caller_fenced` proves the wrong thing | **Not dissolved — narrowed.** The first draft claimed a helper receiving a session "could only have obtained it from a fence." False: `proposals/accepting.py:207` injects a request-owned session into `_insert_citation_locked`. `caller_fenced` is kept and rewritten to require the fence to enclose the ancestor's *whole* critical section, not merely the helper call. |
| **3.** `ingest_bytes` cross-module commits | **Not dissolved — made visible.** `ingest_bytes(session_factory=ctx.session_factory, ...)` becomes an attribute read, so the rule rejects it. F3b addresses it. |

Two of four go away because the open set was never necessary. The third is
narrowed rather than removed, and the fourth is made visible.

## 6. Reads are not fenced

A read takes no lock, checks no generation, and returns whatever it returns.

**Rationale.** A stale read is not a durable effect. A stale read that *informs* a
durable write is caught by that write's fence, because the fence re-checks the
generation at the moment of writing — which is the only moment that matters. Read
revocation for long-lived actors is a separate mechanism and is already settled:
`2026-08-17` §6 gives rooms a lease with room-enforced expiry.

**Consequence, stated rather than engineered around.** A tool can read a workspace
that is deleted microseconds later and show the agent data from a workspace that
no longer exists. The agent's *write* will fail. Bytes already returned to the
model are not recoverable, exactly as §6 states for delivered socket bytes.

## 7. Behaviour on a stale generation

`WorkspaceGenerationStale` propagates out of the fence. The tool layer converts it
into a tool error the agent sees, and the turn continues; the turn's existing
terminal-state handling already covers a workspace that has gone away underneath a
running turn (Slice A, `7b16547`).

**This is a design intent, not a verified claim.** The exact conversion point in
the tool-error path is to be confirmed against the code during planning, and the
plan must name the test that pins it.

## 8. Scope

### F3 — the boundary and the five single-transaction sites

1. `fenced_write` and `fenced_execute` in `fencing.py`; `read_session` on
   `ToolContext`.
2. The detector redesign and the gate rule of §4; deletion of the effect model.
3. Conversion of the five semantic sites that are each one transaction:
   - `tools/citations.py::propose_citation_impl`
   - `tools/proposal.py::_propose_artifact_edit_impl`
   - `tools/review_submit.py::submit_review_impl`
   - `tools/scorecard.py::submit_scorecard_impl`
   - `tools/uncited_claims.py::submit_uncited_claims_impl`
4. The two `accounting` sites in `tools/references.py` — `fetch_reference_impl`
   (`:463`) and its `_refund_fetch_slot` (`:499`) — move to `accounting_execute`,
   and **their predicates narrow from "whichever turn is running" to the
   originating job identity** (§3.1). They are permitted, and for the refund
   required, to run on a stale generation; carrying no generation predicate is
   exactly why ownership must bound them instead.
5. Every remaining acquisition under `tools/` moves to `read_session`.
6. **The runtime two-turn re-stamp test F2 owes.** Drive two turns through one
   retained `ToolContext` across a generation change and assert the second carries
   the new generation. No static check can observe this, and
   `tools/registry.py:80` documents the identical trap for provenance, where a
   stamp missed once leaks the previous turn's value.
7. `SLICE_STATUS["F3"]` flips to `complete` in the same commit that converts the
   last F3 site.

**Blast radius:** 20 acquisition sites across 8 files under `tools/`. 20 of 20 are
the identical `with ctx.session_factory() as s:` shape. 9 of them commit today.

### F3b — the ingest pipeline

`references/ingest.py` performs four phases with **deliberately** separate
transactions: phase 1 commits the `Reference` row on the caller's session
(`:389`), then text extraction and embedding run — slow — and later phases commit
status transitions on fresh sessions (`:121`, `:188`, `:202`, `:219`, `:297`,
`:318`).

**The fresh sessions are correct and must survive.** Holding one fenced
transaction across embedding would pin the commit mutex for the duration of the
slow work — the failure `2026-08-17` §5 exists to prevent, and the reason
`2026-08-17` §10 says *fence what is frequent, barrier what is destructive.*

So F3b teaches each phase to re-check the generation at its own boundary, and must
answer a question F3 does not raise: **what does a stale generation mean for a
half-ingested reference?** The row exists, the text may be extracted, the
embeddings may be partial. Abandoning without writing is prohibited by
`2026-08-17` §7 — it strands rows in `running`. The likely answer is a terminal
status transition that is itself an `accounting` write, but that is F3b's design
work, not a decision taken here.

## 9. What F3 does not do

- **Does not touch `ingest_bytes`.** Its entry stays in the inventory, classified
  and explicitly **not** fenced, and `SLICE_STATUS["F3b"]` stays `pending`.
- **Does not touch the citation-audit write path or the `citation_service`
  capability** (§4.1). Both are classified, pinned, and deferred to F3c.
- **Does not fence reads** (§6).
- **Does not rename `session_factory`** (§4).
- **Does not touch the job families, setup, turn finalizers, or collab rooms** —
  F4–F7, unchanged.
- **Does not remove any compensation.** Step 1 of the four-step sequence
  (fence → prove → remove → relax) is still purely additive. The 6s barriers,
  `reconcile_cancelled_jobs`, and cancel-on-delete all stay until step 3.

## 10. How F3 is proven

**The gate rule is falsifiable.** Writing `with ctx.session_factory() as s:` in any
converted tool must fail the gate. A test asserts this against a fixture, and a
mutation that narrows the rule to a single file must redden it.

**The conversions are proven per-site**, by `2026-08-17` §9's technique: drive the
site with a stale generation and assert no semantic effect landed, **with that
site's compensation disabled in the test.** A conversion test that passes with the
fence removed is measuring the compensation, not the fence. This is the technique
that exposed the decorative tests in C1.

**The retained context is proven at runtime**, per §8 item 6. This is the highest
-risk silent failure in the architecture and it is the one thing F2 could not
observe at all.

**The capability proxy is proven by attempted escape.** Tests assert that
`commit`, `rollback`, `begin` and `close` are absent from both the write and read
capabilities, that a write through the read capability raises, and — the one that
matters — that the advisory lock is **still held** at the end of a fenced block
whose body called every method the proxy does expose.

**The accounting ownership predicate is proven by the interleaving it exists for:**
reserve a slot as generation G, delete and restore the workspace, start a new turn
at G+1, and assert the old actor's refund does not touch the new turn's row.

**Every new rule carries negative fixtures.** A rule with only passing examples is
a decorative test one level up — the failure F2 shipped.

## 11. Risks

- **The capability proxy must not grow an escape hatch.** The first request for
  "just expose the real session for this one case" reopens §3.2 entirely. If a
  case genuinely needs it, it needs a new pinned form, not an accessor.
- **Injected sessions remain the soft edge** (§4.1). The interpreter closes direct
  acquisition; nothing closes a caller outside `tools/` handing a session in. That
  is what `caller_fenced` is for, and its rewritten rule is the only thing
  standing there.
- **`fenced_write` must not become a long-held lock.** It takes the commit mutex.
  Anything slow — network, embedding, LLM calls — must happen outside it. This is
  the same discipline Slice C3 (`df437b4`) bounded with `lock_timeout`, and the
  new form makes it easy to write a long fence by accident.
- **`read_session` is a new default.** It will be reached for reflexively, which
  is fine — it is a read-only capability for its whole
  lifetime (§3.2), so the worst case is a runtime error, not a silent unfenced
  write.
- **`accounting_execute` is the one form that can spread.** It is a legitimate
  unfenced write, so mistaking it for "the easy one" is the failure mode. The
  two-way pin in §3.1 is what stops that, and it must be mutation-proven in both
  directions: an `accounting` site that stops using the form, and a `semantic`
  site that starts.

## 12. Changelog

**Revision 2 (2026-08-18).** A review found five blocking holes in revision 1,
each confirmed against the tree. Four were the same mistake in different places —
**treating a permission as a mechanism.**

- **The yielded `Session` could leave the fenced transaction.** `commit()` inside
  the block drops the transaction-scoped advisory lock and the next statement
  autobegins an unfenced one, inside a Python block that still looks fenced. The
  same defect made `read_session` writable, because `SET TRANSACTION READ ONLY`
  binds to one transaction. Now a restricted capability with no
  transaction-boundary methods (§3.2).
- **The handle stayed public.** An AST rule cannot make a resource unavailable:
  `getattr(ctx, "session_factory")` walks past it, and that idiom is already in
  the tree. `InitVar` gives runtime enforcement at zero churn, so the earlier
  rename-versus-gate trade-off was a false choice (§4).
- **Accounting got a writable session.** "Exempt from the generation check" is not
  "exempt from atomicity". Revision 1's own example, `job.fetch_count += 1`, would
  have *replaced* the correct conditional UPDATE at `references.py:463` with the
  non-atomic read-modify-write that shipped as a defect twice. Now a statement
  form — and its predicate binds to the originating job, since with no generation
  predicate ownership is the only available bound (§3.1).
- **The fences took a caller-supplied generation.** That lets a stale actor fence
  against a freshly read value in perfectly canonical shape. Identity now comes
  from `ctx` and the parameter does not exist (§3.3).
- **The closed set was not closed, and one dissolution claim was false.**
  `accepting.py:207` injects a request-owned session into a `tools/` helper, so
  "its caller could only have obtained that session from a fence" is untrue;
  `caller_fenced` is kept and narrowed rather than deleted. `audit.py:90` and
  `citation_service` are capability holders needing their own slice (§4.1).
- **Staging was contradictory** — an absolute rule shipping with known violations
  present. Resolved by making the exemption an enumerated, expiring property of
  the inventory (§4.2).

**Revision 1 (2026-08-18).** Written after the F2 review. Establishes session
ownership as the fence mechanism, deletes the effect model, splits ingest into
F3b, declines the rename, and records reads as deliberately unfenced.

Self-review then found two holes in this document's own first draft, both of the
same shape — a session obtainable outside a fence:

- **Accounting writes had nowhere to go.** They must land on a stale generation,
  so `fenced_write` is wrong for them and `read_session` cannot serve them.
  The draft said they would use "`read_session` or `fenced_execute` as their
  category dictates", which is not a mechanism. Added `accounting_execute` (§3.1)
  with a two-way pin to the inventory.
- **`read_session` was writable.** Nothing stopped `session.add(); commit()` on
  it, and policing that would have required asking "does this scope contain a
  write?" — the effect model returning through the side door. It is now
  `SET TRANSACTION READ ONLY` (§3.2).
