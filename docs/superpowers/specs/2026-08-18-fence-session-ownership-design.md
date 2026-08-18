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

`src/call_manager/proposal_assist/fencing.py` gains three context managers that
**acquire** the session rather than wrapping one the caller already holds. F1's
`live_at_generation` and `acquire_commit_mutex_at_generation` remain as the
primitives underneath, unchanged.

```python
# Primitive B — mutex + generation. Git, filesystem, or multiple rows that
# must move together. Commits on clean exit; rolls back on exception.
with fenced_write(ctx, workspace_id, generation=gen) as session:
    session.add(ProposedChange(...))

# Primitive A — ONE conditional statement, DB-only. No lock, so no lock-order
# risk. Opens its own short session, applies live_at_generation INSIDE the
# statement, commits.
fenced_execute(ctx, update(TurnJob).values(fetch_count=...),
               workspace_id=ws, generation=gen)

# Accounting — a durable write that is REQUIRED to land on a stale
# generation. Takes no generation because checking one would be wrong.
with accounting_write(ctx, workspace_id) as session:
    job.fetch_count += 1

# Reads — READ ONLY at the database level. A write on this session raises.
with ctx.read_session() as session:
    row = session.get(Workspace, ws)
```

Both fenced forms raise `WorkspaceGenerationStale` (F1) when the workspace is not
live at `generation`.

### 3.1 Why there are three write forms and not one

`accounting_write` exists because `2026-08-17` §2 *permits* and §7 *requires*
accounting writes to land on a stale generation: freeing an active-unique slot and
refunding an admission slot must happen even when the workspace has moved on.
Fencing them would strand jobs and destroy the deliberate late token accounting at
`review/pass_runner.py:164`. Handing them the raw factory instead would reopen the
hole this whole design closes, so they get a form of their own — conspicuously
named, and pinned: **the gate requires a site using `accounting_write` to be
classified `accounting` in the inventory, and every `accounting` site to use it.**
Neither direction is optional, so the exemption cannot spread by imitation.

### 3.2 `read_session` is read-only at the database level

`read_session` issues `SET TRANSACTION READ ONLY`. A write attempted on it raises
from Postgres.

This matters more than it looks. Without it, `read_session` would be a writable
handle obtained outside a fence — the exact thing §2 abolishes — and the only way
to police it would be to ask "does this scope contain a write?", which is the
effect model coming back in through the side door. Enforcing it at the database
costs one statement and keeps the rule free of analysis.

So: a writable session is obtainable from `fenced_write` and `accounting_write`,
and from nowhere else.

`fenced_execute` keeps Primitive A's defining property: it takes **no lock**.
Where a finalizer already holds the job or workspace row `FOR UPDATE`, forcing it
through the mutex would create the J→M and W→M inversions `2026-08-17` §5
documents. This is why there are two forms and not one.

## 4. The gate rule

**One rule, closed and one grep wide:**

> Outside `fencing.py` and `ToolContext.read_session`, the attribute
> `ctx.session_factory` is never read.

The **constructor keyword** `ToolContext(session_factory=sf)` stays legal and
unchanged — it is `ast.keyword`, a different node from the `ast.Attribute` the
rule forbids. All 7 source constructions and all 52 test constructions across 31
files are untouched.

An unfenced write is therefore caught by the gate, not by the interpreter. A
rename to `_session_factory` would upgrade that to an `AttributeError` at the cost
of ~60 files of mechanical churn inside the slice whose point is the fence. It is
explicitly **not** done, and is not deferred work — it is declined.

### 4.1 What this deletes

`tests/fencing/verifiers.py::_is_effect` and every rule built on it are removed.
The detector stops looking for `.commit()`. The inventory's `caller_fenced`
category is removed with its single entry (see §5).

## 5. What dissolves, and what does not

| `2026-08-17` §9.1 gap | Fate under session ownership |
|---|---|
| **1.** Detector blind to the canonical form | **Dissolved.** The detector looks for *acquisitions*. The canonical form **is** an acquisition, so converted code is *more* visible, not less. The perverse incentive inverts. |
| **2.** `_is_effect` enumerates an open set | **Dissolved.** There is no effect model to be incomplete. |
| **4.** `caller_fenced` proves the wrong thing | **Dissolved.** A helper that *receives* a session never acquires one, so it is not a site at all. Its caller could only have obtained that session from a fence. `tools/citations.py::_write_citekey_cache` stops being an inventory entry. |
| **3.** `ingest_bytes` cross-module commits | **Not dissolved — made visible.** `ingest_bytes(session_factory=ctx.session_factory, ...)` becomes an attribute read, so the rule rejects it. F3b addresses it. |

Three of four go away because the open set was never necessary.

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
   and its `_refund_fetch_slot` — move to `accounting_write` (§3.1). They are
   permitted, and for the refund *required*, to run on a stale generation; what
   they may not do is read the raw handle.
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

**Every new rule carries negative fixtures.** A rule with only passing examples is
a decorative test one level up — the failure F2 shipped.

## 11. Risks

- **The acquisition set must stay closed.** The rule catches
  `ctx.session_factory`. A tool that imports a `sessionmaker` directly, or is
  handed a `Session` by a caller outside `tools/`, is outside the rule. F3 must
  add a companion check that no module under `tools/` imports a sessionmaker or
  engine, so the closed set stays closed.
- **`fenced_write` must not become a long-held lock.** It takes the commit mutex.
  Anything slow — network, embedding, LLM calls — must happen outside it. This is
  the same discipline Slice C3 (`df437b4`) bounded with `lock_timeout`, and the
  new form makes it easy to write a long fence by accident.
- **`read_session` is a new default.** It will be reached for reflexively, which
  is fine — it is read-only at the database level (§3.2), so the worst case is a
  runtime error, not a silent unfenced write.
- **`accounting_write` is the one form that can spread.** It is a legitimate
  unfenced write, so mistaking it for "the easy one" is the failure mode. The
  two-way pin in §3.1 is what stops that, and it must be mutation-proven in both
  directions: an `accounting` site that stops using the form, and a `semantic`
  site that starts.

## 12. Changelog

**Revision 1 (2026-08-18).** Written after the F2 review. Establishes session
ownership as the fence mechanism, deletes the effect model, splits ingest into
F3b, declines the rename, and records reads as deliberately unfenced.

Self-review then found two holes in this document's own first draft, both of the
same shape — a session obtainable outside a fence:

- **Accounting writes had nowhere to go.** They must land on a stale generation,
  so `fenced_write` is wrong for them and `read_session` cannot serve them.
  The draft said they would use "`read_session` or `fenced_execute` as their
  category dictates", which is not a mechanism. Added `accounting_write` (§3.1)
  with a two-way pin to the inventory.
- **`read_session` was writable.** Nothing stopped `session.add(); commit()` on
  it, and policing that would have required asking "does this scope contain a
  write?" — the effect model returning through the side door. It is now
  `SET TRANSACTION READ ONLY` (§3.2).
