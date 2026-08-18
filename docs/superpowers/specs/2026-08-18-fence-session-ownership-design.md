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

**Do not enumerate misuse of a resource. Control how the resource is obtained.**

A writable session is obtained from a fence, and it refuses to outlive the fenced
transaction (§3.2). Everything done with it is therefore inside the fence by
construction. There is no effect model, so there is nothing for an effect model to
miss.

The claim is bounded, and §4 states the bound: this closes **direct acquisition
inside `tools/`**. Sessions injected from outside, and objects that own their own
write path, are named in §4.1 and handled by their own slices rather than by this
rule.

This is `web/_writes.py::workspace_write()` carried to its conclusion — that
helper "exists so the rule cannot be written wrongly." The same argument applies
one level down, to the handle itself.

## 3. The four forms

`src/call_manager/proposal_assist/fencing.py` gains the four forms below. They
**acquire** the session rather than wrapping one the caller already holds. F1's
`live_at_generation` and `acquire_commit_mutex_at_generation` remain as the
primitives underneath, unchanged.

```python
# Primitive B — mutex + generation. Identity comes from ctx; there is no
# caller-supplied generation to get wrong.
with fenced_write(ctx) as session:
    session.add(ProposedChange(...))     # commits on clean exit
    session.commit()                     # raises FenceBoundaryViolation

# Primitive A — ONE conditional statement, DB-only, no lock.
fenced_execute(ctx, update(Artifact).where(...).values(...))

# Accounting — NAMED operations that own their predicates (§3.1).
reserve_fetch_slot(ctx)
refund_fetch_slot(ctx)

# Reads — a read-only session for its whole lifetime.
with read_session(ctx) as session:
    row = session.get(Workspace, ws)
```

### 3.1 Accounting is a NAMED OPERATION, not a permitted statement

`2026-08-17` §2 permits and §7 *requires* accounting writes on a stale generation:
freeing an active-unique slot and refunding an admission slot must happen after the
workspace has moved on. Fencing them would strand jobs and destroy the deliberate
late token accounting at `review/pass_runner.py:164`.

But "exempt from the generation check" is not "exempt from atomicity," and it is
not "exempt from ownership." Revision 1 gave accounting a writable session, whose
example — `job.fetch_count += 1` — would have *replaced* the correct conditional
UPDATE at `references.py:463` with the non-atomic read-modify-write that shipped as
a defect twice (`28abbc4`, `701fbb7`). Revision 2 replaced it with a statement form
plus a category pin, which proves an approved function took the exemption but not
that its statement is owned; this passes that gate and reproduces the defect:

```python
accounting_execute(ctx, update(TurnJob).where(
    TurnJob.workspace_id == ctx.workspace_id, TurnJob.status == "running"
).values(fetch_count=TurnJob.fetch_count - 1))     # canonical, and unowned
```

So accounting is not a form callers fill in. It is a **closed set of named
operations** in `fencing/accounting.py`, each owning its own predicate:

```python
def refund_fetch_slot(ctx) -> None:
    _execute(update(TurnJob).where(
        TurnJob.id == ctx.origin_job_id,          # OWNED, not "whichever is running"
        TurnJob.status == "running",
        TurnJob.fetch_count > 0,
    ).values(fetch_count=TurnJob.fetch_count - 1))
```

**Why ownership is load-bearing.** These writes carry no generation predicate by
design, so ownership is the only bound available. Today `references.py:463` and
`:499` target *whichever* turn is running for the workspace: after
delete → restore → a new turn starts, an actor from the old generation reserves or
refunds **the new turn's** slot.

**The gate rule:** an `accounting` inventory entry must resolve to a function in
`fencing/accounting.py`. A new accounting site therefore requires a new named
operation — a diff a human reads — rather than a new call to a general form.

### 3.2 The boundary lives in the Session, not in a wrapper

The yielded object is a `Session` subclass that refuses `commit`, `rollback`,
`begin` and `close` from the body, raising `FenceBoundaryViolation`. The owning
context manager commits through a private path. `read_session` yields the same
class with writes additionally refused.

**It must be the Session itself, not a proxy around one.** Revision 2 specified an
attribute-forwarding proxy exposing `query`, `get` and friends. SQLAlchemy hands
the real session straight back through its own ORM back-references — verified on
this repository's 2.0.49:

```python
q = session.query(Model);  q.session is session          # True
object_session(session.get(Model, id)) is session        # True
```

So `w.query(Model).session.commit()` walks around any wrapper. A test that only
asserts `commit` is absent *from the proxy* is decorative for the property it
names. Enforcing in the class means every back-reference hands back the restricted
object, because it **is** the object.

**What this protects.** The advisory lock is transaction-scoped. A body that
commits drops the lock and the next statement autobegins a **new, unfenced**
transaction inside a Python block that still looks fenced. Not hypothetical:
`tools/citations.py:744` takes `owns_txn: bool` precisely because committing a
received session is an established pattern here. The same leak makes a read-only
session writable, since `SET TRANSACTION READ ONLY` binds to one transaction.

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

## 4. Privacy is the LAST step, and it is a convention with a guard

**Privatisation cannot come first.** Removing `ctx.session_factory` while any
caller still reads it is not a gate failure, it is an `AttributeError` in
production. Four live paths read it today and are deliberately deferred:
`tools/references.py:561` and `:564` (ingest) and `tools/audit.py:78` and `:90`
(citation audit). A pending inventory exemption governs the **gate**; it does not
make the attribute exist. So `fetch_reference` and `validate_citations` would both
break — the latter even on its deterministic path.

The dependency runs one way only: **every acquisition must be converted before the
handle can be taken away.** §8 re-slices accordingly, and privatisation becomes its
own final slice, where it is a one-line change whose correctness is trivially
checkable because nothing reads the attribute any more.

**And privacy is a convention, stated as one.** `InitVar` storing
`self._session_factory` renames the handle; it does not remove it.
`ctx._session_factory()` and `getattr(ctx, "_session_factory")` both work. This
codebase already reaches context capabilities dynamically
(`tools/references.py:529`), so the convention is not self-enforcing. What backs it:

1. The underscore, as a trusted-code convention — this is a repository whose
   contributors read the spec, not a sandbox for untrusted callers.
2. A gate rule covering **both** spellings and dynamic access: no module under
   `tools/` may read `session_factory` or `_session_factory` as an attribute, name
   either in a `getattr`, or import a `sessionmaker` or `Engine`.

The honest claim is therefore: **direct acquisition of a writable session inside
`tools/` is closed by the interpreter for the ordinary spelling and by the gate for
the rest.** It is not a capability-security boundary and this spec does not claim
one.

### 4.1 What the closed set does not contain

- **Sessions injected by callers outside `tools/`.** `proposals/accepting.py:207`
  passes its own request-owned session into `_insert_citation_locked`. A helper
  under `tools/` that *receives* a session has not necessarily received a fenced
  one. `caller_fenced` is retained for exactly this, rewritten to require the fence
  to enclose the ancestor's whole critical section (`2026-08-17` §9.1 gap 4).
- **Capability holders.** `ctx.citation_service` owns its own `_session_factory`
  and commits usage records (`citations/service.py:114`); `tools/audit.py:90` hands
  the factory to the semantic audit engine, which reaches
  `citations/verdict_cache.py::store_verdict`. Each gets a classified inventory
  entry and is converted in its own slice.

## 5. What dissolves, and what does not

| `2026-08-17` §9.1 gap | Fate under session ownership |
|---|---|
| **1.** Detector blind to the canonical form | **Dissolved.** The detector looks for *acquisitions*. The canonical form **is** an acquisition, so converted code is *more* visible, not less. The perverse incentive inverts. |
| **2.** `_is_effect` enumerates an open set | **Dissolved.** There is no effect model to be incomplete. |
| **4.** `caller_fenced` proves the wrong thing | **Not dissolved — narrowed.** The first draft claimed a helper receiving a session "could only have obtained it from a fence." False: `proposals/accepting.py:207` injects a request-owned session into `_insert_citation_locked`. `caller_fenced` is kept and rewritten to require the fence to enclose the ancestor's *whole* critical section, not merely the helper call. |
| **3.** `ingest_bytes` cross-module commits | **Not dissolved — made visible.** `ingest_bytes(session_factory=ctx.session_factory, ...)` becomes an attribute read, so the rule rejects it. F6 addresses it. |

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

## 8. Scope — five slices, in dependency order

Revision 2 drew F3 as "primitives + boundary + five sites", which cannot execute:
it privatised a handle four live paths still read, and it fenced writes using two
identities the retained context does not carry. The order below makes both
impossible rather than merely fixed.

### F3 — turn identity *(prerequisite; additive; no fence, no handle change)*

Nothing can be fenced until the interactive actor knows **which generation it
believes in** and **which job it owns**. Today it knows neither:

- `turns/processor.py:215` claims the turn with
  `UPDATE ... SET status='running' WHERE id=:id AND status='queued'` and neither
  sets nor returns `claimed_at_generation`. F1 added that column to seven tables
  and nothing writes it yet.
- `set_turn_provenance` (`drafting/claude_adapter.py:725`) stamps
  `triggering_turn_id`, `triggering_prompt`, `created_by`, `origin_session_id` —
  no generation, and no `turn_job_id`. `registry.py:87` leaves `origin_job_id`
  `None` for interactive contexts by design.

F3 therefore: makes the claim set **and return** `claimed_at_generation` in the
same statement, plumbs that generation and the `turn_job_id` through
`set_turn_provenance` onto the retained `ToolContext`, and proves the per-turn
re-stamp with the runtime two-turn test F2 owes — drive two turns through one
retained context across a generation change and assert the second carries the new
generation.

**The generation must come from the claim, not from a later read.** Reading it
afterwards reopens the exact race the fence exists to close: delete → restore
between claim and read, and the old actor adopts G+1 as its belief.

### F4 — the forms *(additive; nothing converted)*

`FencedSession`, `fenced_write`, `fenced_execute`, `read_session`, and
`fencing/accounting.py` with its named operations. Added and tested against
fixtures; no call site moves, so the branch stays inert in the F1 sense.

### F5 — convert the five single-transaction sites

`propose_citation_impl`, `_propose_artifact_edit_impl`, `submit_review_impl`,
`submit_scorecard_impl`, `submit_uncited_claims_impl`, plus the two accounting
sites in `references.py` (`:463`, `:499`) moving to the named operations with
owned predicates. Every other acquisition under `tools/` moves to `read_session`.

### F6 — the ingest pipeline

`references/ingest.py` runs four phases in **deliberately** separate transactions:
phase 1 commits the `Reference` row on the caller's session (`:389`), then text
extraction and embedding run — slow — and later phases commit status transitions on
fresh sessions (`:121`, `:188`, `:202`, `:219`, `:297`, `:318`).

**The fresh sessions are correct and must survive.** One fenced transaction across
embedding would pin the commit mutex for the duration — the failure `2026-08-17` §5
exists to prevent, and why §10 says *fence what is frequent, barrier what is
destructive*. So each phase re-checks at its own boundary, and F6 must answer a
question no earlier slice raises: **what does a stale generation mean for a
half-ingested reference?** The row exists, text may be extracted, embeddings may be
partial. Abandoning without writing is prohibited by §7 — it strands rows in
`running`. The likely answer is a terminal status transition that is itself an
accounting operation, but that is F6's design work.

### F7 — capability holders

`tools/audit.py` and the citation-audit write path
(`citations/verdict_cache.py::store_verdict`), and the `citation_service` usage
records (`citations/service.py:114`).

### F8 — privatisation

Only once F5–F7 have converted every acquisition. `InitVar` storage plus the
two-spelling gate rule of §4. One line of production change, and its correctness
is checkable by the absence of readers rather than by reasoning about them.

## 9. What this design does not do

- **Does not fence reads** (§6).
- **Does not claim a capability-security boundary** (§4). Underscore privacy is a
  trusted-code convention with a gate behind it.
- **Does not cover injected sessions or capability holders** by the acquisition
  rule (§4.1); those are `caller_fenced` entries and F7 respectively.
- **Does not touch the job families, setup, turn finalizers, or collab rooms** —
  `2026-08-17` F4–F7, unchanged and renumbered around these slices.
- **Does not remove any compensation.** Step 1 of fence → prove → remove → relax
  is still purely additive. The 6s barriers, `reconcile_cancelled_jobs` and
  cancel-on-delete all stay until step 3.
- **Does not privatise the handle until F8.** Doing it earlier is an
  `AttributeError` in production, not a gate failure (§4).

## 10. How each slice is proven

**The claim generation is proven by the race it closes** (F3). Interleave a
delete → restore between the claim and the first tool write, and assert the actor
still believes in its claimed generation rather than the current one. A test that
reads the generation after the claim passes whether or not the fix is present, so
it must be driven through the claim statement itself.

**The retained context is proven at runtime** (F3). Two turns through one context
across a generation change; the second must carry the new value. No static check
can observe a per-turn re-stamp, and `registry.py:80` documents the identical trap
for provenance, where a stamp missed once leaks the previous turn's value.

**The session boundary is proven by attempted escape** (F4), through the ORM
back-references and not merely on the object handed out:

```python
with fenced_write(ctx) as session:
    with pytest.raises(FenceBoundaryViolation):
        session.query(Model).session.commit()
    with pytest.raises(FenceBoundaryViolation):
        object_session(session.get(Model, some_id)).commit()
assert advisory_lock_was_held_throughout()
```

The last line is the one that matters: a test asserting only that `commit` raises
proves nothing about whether the lock survived.

**The accounting operations are proven by the interleaving they exist for** (F5):
reserve a slot as generation G, delete and restore the workspace, start a new turn
at G+1, and assert the old actor's refund does not touch the new turn's row.

**Each conversion is proven per-site** (F5–F7) by `2026-08-17` §9's technique:
drive the site with a stale generation and assert no semantic effect landed, **with
that site's compensation disabled in the test.** A conversion test that passes with
the fence removed is measuring the compensation. This is the technique that exposed
the decorative tests in C1.

**Privatisation is proven by absence** (F8): no reader of either spelling remains,
which is a search, not an argument.

**Every new rule carries negative fixtures.** A rule with only passing examples is a
decorative test one level up — the failure F2 shipped.

## 11. Risks

- **The slices must land in order, and F8 must land.** If F5–F7 ship and F8 never
  does, the handle stays public and new code can reach it — the gate catches that,
  but only while someone keeps the inventory honest. An unfinished sequence is the
  most likely way this design decays.
- **`FencedSession` must not grow an escape hatch.** The first request for "just
  expose the real session for this one case" reopens §3.2 entirely. A genuine case
  needs a new named form, not an accessor.
- **`fenced_write` must not become a long-held lock.** It takes the commit mutex,
  so anything slow — network, embedding, LLM calls — must sit outside it. Slice C3
  (`df437b4`) bounded this with `lock_timeout`; the new form makes a long fence
  easy to write by accident, and F6 is where that temptation is strongest.
- **Injected sessions remain the soft edge** (§4.1). The interpreter closes direct
  acquisition; nothing closes a caller outside `tools/` handing a session in.
  `caller_fenced` is the only thing standing there, and its rule is the one F2 got
  wrong.
- **Named accounting operations can still be written wrong.** The form guarantees a
  human reviews each one; it does not guarantee the predicate is owned. Every
  operation needs the F5 interleaving test, not just the two that exist today.

## 12. Changelog

**Revision 3 (2026-08-18).** A second review found four blocking holes and one
overstated guarantee in revision 2. Two were a species revision 2 did not contain
at all — **the slice could not execute**:

- **F3 privatised a handle four live paths still read.** `references.py:561`/`:564`
  and `audit.py:78`/`:90` would raise `AttributeError` in production;
  `validate_citations` would fail even on its deterministic path. A pending
  inventory exemption governs the gate, not the interpreter. Privatisation is now
  the **last** slice (§4, §8).
- **The retained context carries neither identity the new APIs need.**
  `origin_job_id` is `None` for interactive contexts by design, `set_turn_provenance`
  passes no `turn_job_id`, and the claim at `processor.py:215` neither sets nor
  returns `claimed_at_generation` — F1's column is still unwritten. Reading the
  generation after the claim reopens the delete/restore race. Now the first slice,
  F3 (§8).

The remaining three were the same "permission, not mechanism" error revision 2
failed to finish removing:

- **The capability proxy leaked the real `Session`.** Verified on this repo's
  SQLAlchemy 2.0.49: `query(...).session is session` and
  `object_session(instance) is session`. So `w.query(M).session.commit()` walks
  around any wrapper. The boundary moves into a `Session` subclass (§3.2).
- **Accounting ownership was a category pin.** It proved an approved function took
  the exemption, not that its statement was owned — a later site could be
  canonically shaped and unowned. Accounting is now a closed set of named
  operations that own their predicates (§3.1).
- **`InitVar` only renames the handle.** `ctx._session_factory()` still works, as
  does `getattr`. Stated as a trusted-code convention with a gate covering both
  spellings and dynamic access, rather than claimed as unobtainability (§4).

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
