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
operations** in a new sibling module `proposal_assist/fencing_accounting.py`, each
owning its own predicate. It is a sibling and not `fencing/accounting.py` because
`proposal_assist/fencing.py` is a module, not a package: making it one would mean
converting the file and preserving every existing import, which is not the additive
change this describes.

```python
def refund_fetch_slot(ctx) -> None:
    _execute(update(TurnJob).where(
        TurnJob.id == ctx.active_turn_job_id,     # OWNED, not "whichever is running"
        TurnJob.status == "running",
        TurnJob.fetch_count > 0,
    ).values(fetch_count=TurnJob.fetch_count - 1))
```

**Why ownership is load-bearing.** These writes carry no generation predicate by
design, so ownership is the only bound available. Today `references.py:463` and
`:499` target *whichever* turn is running for the workspace: after
delete → restore → a new turn starts, an actor from the old generation reserves or
refunds **the new turn's** slot.

**The gate rule needs two identities, not one.** An `accounting` inventory entry
names a *caller site* — `references.py::fetch_reference_impl` — which by definition
does not live in `fencing_accounting.py`. So the rule is:

> Every `accounting` entry names an `operation`, that operation must resolve to a
> function in `fencing_accounting.py`, and the entry's own scope must contain a
> call to it.

Tracking only the operation would make a new call site invisible; tracking only the
caller would not pin which operation it uses. A new accounting site therefore
requires either a new named operation or a new classified entry — a diff a human
reads, either way.

**Why a separate `active_turn_job_id`.** It cannot reuse `origin_job_id`:
`registry.py:87` leaves that field `None` for interactive contexts *by design*, and
`tools/proposal.py:169` copies it into durable provenance. Populating it would
silently change stored data, so this slice would not be additive.

### 3.2 The fence OBSERVES its transaction; it does not restrict the Session

`fenced_write` and `read_session` hand out an ordinary `Session`. At entry the
fence stamps a transaction-scoped token:

```sql
SET LOCAL call_manager.fence_token = '<uuid4>'
```

Before every write the fence performs, and again at exit, it re-reads
`current_setting('call_manager.fence_token', true)` and compares. `SET LOCAL` dies
with its transaction, so **any** commit, rollback or reconnection — by any route,
named or unnamed — clears it, and the fence raises `FenceBoundaryViolation`.

**Why observation and not restriction.** Three revisions of this document tried to
close the Session's API by enumeration, and each was defeated by paths the next
review found. Verified against this repository's SQLAlchemy 2.0.49, an
override-only subclass is escaped by all of:

```python
session.get_transaction().commit()      # ESCAPED
session.connection().commit()           # ESCAPED
session.execute(text("COMMIT"))         # ESCAPED
session.get_bind()                      # is the Engine itself
```

A forwarding proxy fares no better: `query(...).session is session` and
`object_session(instance) is session`, so the ORM hands the real object back
through its own return values. **The set of escapes is open**, and closing an open
set by listing it is the mistake F2's verifier made.

The token inverts the problem. It does not prevent an escape — it makes an escaped
fence **fail loudly instead of committing silently**, and it does so for escape
paths nobody enumerated, because it observes the invariant rather than its
violations. For `fenced_write` the advisory lock is a second, independent witness:
it is transaction-scoped, so a surviving lock is proof the transaction survived,
and the fence asserts it still holds at exit.

`SET LOCAL` is already the idiom here — Slice C3 (`df437b4`) bounds every mutex
tail with `SET LOCAL lock_timeout`.

**The cost, stated.** One cheap `current_setting` round-trip per fenced write. A
body that escapes and then performs no further fenced write is caught at exit
rather than at the moment of escape, so the error names the block, not the line.

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
| **3.** `ingest_bytes` cross-module commits | **Not dissolved — made visible.** `ingest_bytes(session_factory=ctx.session_factory, ...)` becomes an attribute read, so the rule rejects it. F3e addresses it. |

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

## 8. Scope — six sub-slices of F3, in dependency order

**The numbering must not move.** `2026-08-17` §11 already assigns F4–F9 distinct
meanings (background job families, setup, turn jobs, collab writes, durable read
checks, the room lease), and the merged gate encodes those meanings:
`tests/fencing/inventory.py:20` slices every `claim_fn` as `F4` and everything else
as `F3`, and `SLICE_STATUS` knows only those two. Revision 3 redefined F3, F4 and
F5, which would have made `SLICE_STATUS["F3"] = "complete"` demand that every tool
site be fenced before the forms existed.

So the work below are **sub-slices of F3**: F3a … F3f. F4–F9 keep their meanings
untouched.

### The inventory migration (F3a, first commit)

`SLICE_STATUS` becomes `{F3a…F3f: pending, F4…F9: pending}` and every row's `slice`
is reassigned:

| Rows | From | To | Because |
|---|---|---|---|
| 6 × `claim_fn` | `F4` | **F3a** | recording the generation at claim is F3a's work; architecture-F4 is *fencing the job families' effects*, a different concern the inventory does not yet track |
| 5 × `tool_context` | `F3` | **F3a** | a context carries the generation only once its actor records one |
| 7 × `tool_commit` (the 5 semantic + 2 accounting) | `F3` | **F3d** | converted there |
| 1 × `tool_commit` (`_do_fetch_and_ingest`) | `F3` | **F3e** | ingest |
| 1 × `tool_commit` (`_write_citekey_cache`) | `F3` | **F3d** | `caller_fenced`; moves with its ancestor |

`regenerate_inventory.py` gains a one-off slice remap, and the F2 rename guard
already refuses to lose a `fenced: true` row across it — all 20 are `false`, so the
migration is safe by inspection.

### F3a — claim identity, every actor family *(additive; nothing fenced)*

Nothing can be fenced until each originating actor knows **which generation it
believes in**. Today none does, and this is why F3a covers all families at once
rather than the interactive one alone: `submit_scorecard_impl` is called by the
*evaluator*, whose context is one-shot, so converting that tool while only the
interactive path had identity would fence against a generation that does not exist.

- **The interactive claim** (`turns/processor.py:215`) sets **and returns**
  `claimed_at_generation` in the same statement. F1 added the column to seven
  tables and nothing has written it since.
- **The four background claims** — coherence, evaluator, review, uncited-claims —
  do the same, and their one-shot `ToolContext` constructions
  (`coherence/agent.py:42`, `evaluator/agent.py:41`, `review/agent.py:37`,
  `uncited_claims/agent.py:37`) carry it at construction, which F2's inventory
  already records as required for `ctx_lifetime: one_shot`.
- **`set_turn_provenance`** (`drafting/claude_adapter.py:725`) gains the generation
  and a new `active_turn_job_id` (§3.1), stamped unconditionally every turn.
- **The runtime two-turn test** F2 owes: two turns through one retained context
  across a generation change; the second must carry the new value.

**The generation must come from the claim statement, not a read after it.**
Reading it afterwards reopens the exact race the fence exists to close —
delete → restore lands between claim and read, and the old actor adopts G+1 as its
own belief.

### F3b — the forms *(additive; nothing converted)*

`fenced_write`, `fenced_execute`, `read_session`, the `FenceBoundaryViolation`
token machinery of §3.2, and `fencing_accounting.py` with its named operations.
Tested against fixtures; no call site moves, so the branch is inert in the F1 sense.

### F3c — the gate rule

The detector switches from `.commit()` to acquisitions, `_is_effect` and its rules
are deleted, `caller_fenced` is rewritten to require the fence to enclose the
ancestor's whole critical section, and the two-identity accounting rule of §3.1
lands. Still nothing converted, so the gate is proven against fixtures before it
has live data.

### F3d — convert the tool sites

The five semantic sites — `propose_citation_impl`, `_propose_artifact_edit_impl`,
`submit_review_impl`, `submit_scorecard_impl`, `submit_uncited_claims_impl` — plus
the two accounting sites (`references.py:463`, `:499`) moving to named operations
with owned predicates, and `_write_citekey_cache` with its ancestor. Every other
acquisition under `tools/` moves to `read_session`.

### F3e — the ingest pipeline

`references/ingest.py` runs four phases in **deliberately** separate transactions:
phase 1 commits the `Reference` row on the caller's session (`:389`), then text
extraction and embedding run — slow — and later phases commit status transitions on
fresh sessions (`:121`, `:188`, `:202`, `:219`, `:297`, `:318`).

**The fresh sessions are correct and must survive.** One fenced transaction across
embedding would pin the commit mutex for its duration — the failure `2026-08-17` §5
exists to prevent, and why §10 says *fence what is frequent, barrier what is
destructive*. So each phase re-checks at its own boundary, and F3e must answer a
question no earlier sub-slice raises: **what does a stale generation mean for a
half-ingested reference?** The row exists, text may be extracted, embeddings may be
partial. Abandoning without writing is prohibited by §7 — it strands rows in
`running`. The likely answer is a terminal status transition that is itself an
accounting operation, but that is F3e's design work.

### F3f — capability holders, then privatisation

`tools/audit.py` and the citation-audit write path
(`citations/verdict_cache.py::store_verdict`), and the `citation_service` usage
records (`citations/service.py:114`). **Privatisation lands last, in this
sub-slice's final commit**, once no reader of either spelling remains.

## 9. What this design does not do

- **Does not fence reads** (§6).
- **Does not claim a capability-security boundary** (§4). Underscore privacy is a
  trusted-code convention with a gate behind it.
- **Does not cover injected sessions or capability holders** by the acquisition
  rule (§4.1); those are `caller_fenced` entries and F3f respectively.
- **Does not touch the job families, setup, turn finalizers, or collab rooms** —
  `2026-08-17` F4–F9, whose numbering and meanings are untouched (§8).
- **Does not remove any compensation.** Step 1 of fence → prove → remove → relax
  is still purely additive. The 6s barriers, `reconcile_cancelled_jobs` and
  cancel-on-delete all stay until step 3.
- **Does not privatise the handle until F3f's last commit.** Doing it earlier is
  an `AttributeError` in production, not a gate failure (§4).
- **Does not renumber F4–F9** (§8). The merged gate and `2026-08-17` §11 both
  encode their current meanings.
- **Does not prevent a Session escaping its fence** — it detects one (§3.2). The
  set of escape routes is open; the invariant is not.

## 10. How each slice is proven

**The claim generation is proven by the race it closes** (F3a). Interleave a
delete → restore between the claim and the first tool write, and assert the actor
still believes in its claimed generation rather than the current one. A test that
reads the generation after the claim passes whether or not the fix is present, so
it must be driven through the claim statement itself.

**The retained context is proven at runtime** (F3a). Two turns through one context
across a generation change; the second must carry the new value. No static check
can observe a per-turn re-stamp, and `registry.py:80` documents the identical trap
for provenance, where a stamp missed once leaks the previous turn's value.

**The transaction invariant is proven by real escapes, not by absent methods**
(F3b). A test that asserts `commit` raises proves nothing about whether the
transaction survived. Each verified escape path must be driven through the fence
and must raise:

```python
for escape in (
    lambda s: s.get_transaction().commit(),
    lambda s: s.connection().commit(),
    lambda s: s.execute(text("COMMIT")),
    lambda s: s.query(Model).session.commit(),
    lambda s: object_session(s.get(Model, pk)).commit(),
):
    with pytest.raises(FenceBoundaryViolation):
        with fenced_write(ctx) as session:
            escape(session)
            session.add(Model())        # the token check fires here
```

And the mutation that matters: **remove the token check and every one of these must
go red.** If any stays green, that path was being caught by something incidental.

**The accounting operations are proven by the interleaving they exist for** (F3d):
reserve a slot as generation G, delete and restore the workspace, start a new turn
at G+1, and assert the old actor's refund does not touch the new turn's row.

**Each conversion is proven per-site** (F3d–F3f) by `2026-08-17` §9's technique:
drive the site with a stale generation and assert no semantic effect landed, **with
that site's compensation disabled in the test.** A conversion test that passes with
the fence removed is measuring the compensation. This is the technique that exposed
the decorative tests in C1.

**Privatisation is proven by absence** (F3f): no reader of either spelling remains,
which is a search, not an argument.

**Every new rule carries negative fixtures.** A rule with only passing examples is a
decorative test one level up — the failure F2 shipped.

## 11. Risks

- **The sub-slices must land in order, and F3f must land.** If F3d–F3e ship and
  F3f never does, the handle stays public and new code can reach it — the gate
  catches that, but only while someone keeps the inventory honest. An unfinished
  sequence is the most likely way this design decays.
- **The token is checked, not enforced.** A fenced block that escapes and then
  performs no further fenced write is caught at exit, so the error names the block
  rather than the offending line. That is a debuggability cost, accepted in
  exchange for covering escape routes nobody has enumerated.
- **The token check must not be made optional.** A `skip_token_check=True` kwarg
  for "this one hot path" reopens §3.2 entirely. If a path genuinely cannot afford
  the round-trip, it needs measurement and its own named form, not a flag.
- **`fenced_write` must not become a long-held lock.** It takes the commit mutex,
  so anything slow — network, embedding, LLM calls — must sit outside it. Slice C3
  (`df437b4`) bounded this with `lock_timeout`; the new form makes a long fence
  easy to write by accident, and F3e is where that temptation is strongest.
- **Injected sessions remain the soft edge** (§4.1). The interpreter closes direct
  acquisition; nothing closes a caller outside `tools/` handing a session in.
  `caller_fenced` is the only thing standing there, and its rule is the one F2 got
  wrong.
- **Named accounting operations can still be written wrong.** The form guarantees a
  human reviews each one; it does not guarantee the predicate is owned. Every
  operation needs the F3d interleaving test, not just the two that exist today.

## 12. Changelog

**Revision 4 (2026-08-18).** A third review found three blockers and two
specification defects. Two blockers were of the "cannot execute" species again, and
one was the same enumeration failure for the third time.

- **The Session boundary failed by enumeration, three times running.** Revision 1
  handed out a raw `Session`; revision 2 a forwarding proxy, defeated by
  `query(...).session`; revision 3 a subclass with overrides, defeated by
  `get_transaction().commit()`, `connection().commit()`, `execute(text("COMMIT"))`
  and `get_bind()` — all four reproduced on this repository's 2.0.49. **The set of
  escapes is open**, and closing an open set by listing it is exactly the mistake
  F2's verifier made. Revision 4 stops restricting and starts observing: a
  transaction-scoped `SET LOCAL` token, checked before every fenced write and at
  exit, catches every escape route including the ones nobody has thought of (§3.2).
- **F3d would have converted background tools before their contexts had a
  generation.** `submit_scorecard_impl` is called by the evaluator, whose context
  is one-shot; revision 3 gave identity to the interactive path only. Claim
  identity now covers all five actor families in one sub-slice, before any
  conversion (§8, F3a).
- **The renumbering collided with the merged gate.** `inventory.py:20` slices every
  `claim_fn` as `F4` and the rest as `F3`, and `2026-08-17` §11 gives F4–F9
  distinct meanings. Revision 3's F3/F4/F5 would have made
  `SLICE_STATUS["F3"] = "complete"` demand fenced tool sites before the forms
  existed. The work is now **sub-slices F3a–F3f**, with an explicit row-by-row
  inventory migration (§8). Revision 3 also said "five slices" over six.
- **`origin_job_id` could not carry the turn identity.** `registry.py:87` leaves it
  `None` for interactive contexts by design and `tools/proposal.py:169` copies it
  into durable provenance, so populating it would change stored data and the slice
  would not be additive. A separate `active_turn_job_id` (§3.1).
- **`fencing/accounting.py` could not exist as an additive file** —
  `proposal_assist/fencing.py` is a module, not a package. It is a sibling module.
  And the accounting gate rule needed **two** identities: the classified entry names
  a caller site, which by definition does not live in the operations module, so the
  rule pins the operation, its module, and the call (§3.1).

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
