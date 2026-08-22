# Fence F3 — Ports, not Handles

**Status:** agreed 2026-08-18 (revision 8); **amended 2026-08-21 (revision 9)** while
planning F3d3b. Mechanism spec for sub-slices F3a–F3f.

Revision 9 changes two things, both marked inline with a dated note: §7's row
allocation moves `_write_citekey_cache` from F3d3 to F3e, and §7's F3e section is
widened from "the ingest pipeline" to "the ingest pipeline, and the mechanism-2
effects". The reason is measured, not stylistic — see the note at the allocation.
Revision 8's claim that `caller_fenced` retires with the F3d move is corrected there
too; F3d3a made the move and it did not.

**Companion:** `2026-08-17-lifecycle-fence-design.md` is the architecture — why a
fence exists, what the invariant is, and the F1–F9 slice breakdown. This document
is the mechanism: how a fenced effect is *expressed* and how coverage is
*enforced*. It supersedes that spec's §3.4 containment approach and closes the
gaps its §9.1 records.

---

## 1. What five attempts established

F2 (`9dc820d`) shipped a gate asking **"is every write inside a fence?"** Writes
are an open set, so the enumeration could never be complete, and a review found
five reachable bypasses.

Revisions 1–4 of this document then tried four times to hand tool code a
`Session` while guaranteeing the transaction could not be misused. Each failed,
and each failed empirically, on this repository's SQLAlchemy 2.0.49:

| Attempt | Defeated by |
|---|---|
| Raw `Session` | `commit()` drops the transaction-scoped advisory lock; the next statement autobegins unfenced |
| Forwarding proxy | `query(...).session is session`, `object_session(instance) is session` — the ORM returns the real object through its own values |
| `Session` subclass with overrides | `get_transaction().commit()`, `connection().commit()`, `execute(text("COMMIT"))`, and `get_bind()` is the `Engine` |
| Transaction token (`SET LOCAL`) | Detects an escape *after* the effect is durable. An exception cannot retract a row, a git commit, or a file |

**The diagnosis is not "find the right wrapper."** A `Session` is **ambient
authority**: it exposes transactions, connections, engines, raw SQL, ORM
back-references, events and internal state. There is always another door. Five
findings in a row are evidence the abstraction boundary is wrong, not that a fifth
restriction would have held.

A separate defect ran through all four and no review caught it until the last:
**a database transaction cannot roll back git.** Every revision's `fenced_write`
promised atomicity across DB, git and filesystem that it could never deliver.

## 2. The principle

**Tools receive capabilities, never handles.**

A tool cannot misuse a `Session` it was never given. It calls named operations that
each own their predicate, their transaction and their commit — code written once,
reviewed once, and tested against a stale generation once.

```
tool
 ├── queries.get_reference(...)          -> a detached DTO
 ├── effects.propose_edit(stamp, cmd)    -> the operation owns the fence
 └── accounting.reserve_fetch_slot(job_id)
```

```python
# In the tool — no session, no model, no statement.
effects.submit_scorecard(invocation.stamp, SubmitScorecard(...))

# In the operation — fixed, reviewed, and the caller never sees this.
# The generation predicate governs the WRITE (§3.5). It is never a
# separate pre-check.
def submit_scorecard(stamp: FenceStamp, cmd: SubmitScorecard) -> None:
    with _sessions.begin() as session:
        # DB-only path: shared lease, THEN the predicate-bearing write (§3.5).
        # Never shared-then-exclusive — that is the one deadlock shape.
        acquire_shared_lease(session, stamp.workspace_id)
        row_count = session.execute(
            update(EvaluatorJob)
            .where(
                EvaluatorJob.id == stamp.origin_job_id,
                EvaluatorJob.status == "running",
                live_at_generation(stamp.workspace_id, stamp.generation),
            )
            .values(scorecard=cmd.scorecard)
        ).rowcount
    if row_count == 0:
        raise WorkspaceGenerationStale(...)
```

This is the same argument as `web/_writes.py::workspace_write()` — *"exists so the
rule cannot be written wrongly"* — applied at the layer where it actually closes:
the tool has nothing to write wrongly with.

### 2.1 What the generation model contributes, unchanged

Nothing below this line is redesigned. It is the strongest part of the
architecture and revisions 1–4 never threatened it:

- Claims capture `claimed_at_generation` atomically.
- Actors carry that belief, immutably, for their lifetime.
- Semantic operations compare it against the workspace's current generation.
- Accounting uses owner-bound conditional writes, deliberately unfenced.
- A stale actor produces no semantic effect.

## 3. The three port kinds

### 3.1 Queries — detached DTOs

`proposal_assist/ports/queries.py`. Every method opens its own read-only session
and returns a **frozen dataclass**, never an attached ORM instance.

Detachment is load-bearing, not tidiness. An attached instance carries
`object_session(instance)`, which is the full `Session` again — the exact leak that
defeated revision 2's proxy. A DTO carries data.

Reads are **not** fenced (§5).

### 3.2 Effects — commands that own their fence

`proposal_assist/ports/effects.py`. One method per semantic operation, each taking
an immutable `FenceStamp` and a command object. The method owns the session, the
generation check, the transaction and the commit.

There is **no** generic `fenced_write(...) as session`, and **no** public
`fenced_execute(ctx, arbitrary_statement)`. Both are ways to hand back the
authority this design removes: the first a session, the second the ability to
compose any statement against any table.

**Multi-resource effects get a narrow operation, not a generic wrapper.** Where an
effect spans DB and git — `_insert_citation_locked` writes `proposal.md` and
`citations.json` in one git commit and then rows — the operation owns the *entire
known sequence* under the mutex. It does not offer a transaction the caller fills
in, because §1's last point applies: the DB transaction cannot roll back the git
commit, so only a fixed, reviewed ordering can be correct. Where such an effect can
be asynchronous, the better shape is a durable intent plus an idempotent worker,
which `2026-08-17` §7 already names as a step-3 prerequisite.

### 3.3 Accounting — owner-bound, deliberately unfenced

`proposal_assist/ports/accounting.py`. `2026-08-17` §2 permits and §7 *requires*
these to land on a stale generation: freeing an active-unique slot and refunding an
admission slot must happen after the workspace has moved on. Fencing them would
strand jobs and destroy the deliberate late token accounting at
`review/pass_runner.py:164`.

"Exempt from the generation check" is not "exempt from atomicity" and not "exempt
from ownership." Each operation is one conditional statement bound to the identity
that originated it:

```python
def refund_fetch_slot(active_turn_job_id: uuid.UUID) -> None:
    _execute(update(TurnJob).where(
        TurnJob.id == active_turn_job_id,        # OWNED, not "whichever is running"
        TurnJob.status == "running",
        TurnJob.fetch_count > 0,
    ).values(fetch_count=TurnJob.fetch_count - 1))
```

Ownership is load-bearing **because** there is no generation predicate: today
`references.py:463` and `:499` target *whichever* turn is running for the
workspace, so after delete → restore → a new turn starts, an actor from the old
generation reserves or refunds **the new turn's** slot.

A non-atomic `job.fetch_count += 1` is prohibited here as everywhere — that defect
shipped twice (`28abbc4`, `701fbb7`) and is Global Constraint 3 of the F1 plan.

### 3.5 Each operation states WHICH atomic mechanism it uses

A named port does not make its internal fence atomic. Revision 5's example checked
the generation and then wrote — two statements, two snapshots under READ COMMITTED,
and a delete/restore fits between them. That is the defect F1's Global Constraint 3
names and that shipped here twice (`28abbc4`, `701fbb7`). There is no
`require_live_generation` primitive and this design does not invent one.

**The predicate alone is not enough.** `live_at_generation` is an `exists()`
subquery (`fencing.py:100`). It takes **no lock on the workspace row**, so under
READ COMMITTED it evaluates against the statement's snapshot and a delete/restore
committing after that snapshot is invisible to it. Reproduced on PostgreSQL 16: a
generation-G `INSERT … SELECT … WHERE live_at_generation(...)` qualifies, pauses
inside the statement, a second connection commits G+2, and the G effect lands
durably against a workspace now at G+2.

For an `UPDATE` this is masked today — PostgreSQL re-checks the `WHERE` against the
updated row version when a concurrent transaction touches the **same target row**,
which cancel-on-delete does. **That is compensation the architecture removes in
step 3**, so the masking is temporary. An `INSERT` has no target row and is exposed
now.

**Each operation selects exactly ONE path and never upgrades between them:**

| Path | Lock |
|---|---|
| DB-only effect | `pg_advisory_xact_lock_shared`, then the predicate-bearing write |
| Multi-resource (git / filesystem / multi-row) | the **exclusive** mutex directly — never shared first |
| Transaction already holds the `Workspace` row `FOR UPDATE` | no advisory lock |

Taking shared and later attempting to upgrade to exclusive is the one deadlock
shape this protocol has: two operations each holding shared, each waiting for the
other to release before either can upgrade. An operation that will touch git or the
filesystem takes the exclusive lock **first**, before its first effect of any kind.

**The DB-only path:**

```python
# effects — concurrent with each other, exclusive against lifecycle
SELECT pg_advisory_xact_lock_shared(<workspace mutex key>)
...effect statement, still carrying live_at_generation(...) in its WHERE...

# delete / restore / any lifecycle transition — the EXISTING exclusive lock
SELECT pg_advisory_xact_lock(<workspace mutex key>)
```

Shared locks do not conflict with one another, so effects stay concurrent. A
lifecycle transition needs the exclusive lock, so it either waits for in-flight
effects to commit or the effect waits and then observes the new generation. The
predicate remains — it is what turns "I waited" into "I checked."

**Every effect operation declares which mechanism it uses:**

| Effect shape | Mechanism |
|---|---|
| DB `UPDATE` / `DELETE` | shared lease **+** `live_at_generation(...)` in the statement's `WHERE`; inspect `rowcount` / `RETURNING`. |
| DB `INSERT` | shared lease **+** `INSERT … SELECT … WHERE live_at_generation(...)`. |
| The txn already holds the **`Workspace`** row `FOR UPDATE` | assert on that row; no further lock. **Only the Workspace row qualifies** — see below. |
| Git, filesystem, or multiple rows moving together | `acquire_commit_mutex_at_generation()` (the **exclusive** mutex) before the first effect, held through the transaction. |

**Mechanism 3 is restricted to the `Workspace` row, as `2026-08-17` §3.1 states.**
Revision 6 widened it to any already-locked target row — an `EvaluatorJob`, an
`UncitedClaimsJob`, a review item. Those rows do not carry the workspace's
lifecycle state, so locking one proves nothing about the generation; what makes it
appear safe today is, again, cancel-on-delete touching the same row.

The fourth is the only one that takes the EXCLUSIVE lock, and it is the only one where the
generation check and the effect are separated in time — which is precisely why it
holds the mutex across the gap, and why `2026-08-17` §5's lock ordering binds it.

### 3.4 `FenceStamp`

A frozen `(workspace_id, generation, active_turn_job_id, origin_job_id)` record.

**It is snapshotted per tool INVOCATION, at dispatch, before any `await` or thread
handoff — never read from the retained context at the moment of the effect.**

The retained interactive `ToolContext` is *mutated* every turn by
`set_turn_provenance`, so reading the generation late reads a **newer** one:

```
1. a tool invocation at generation G begins, and blocks in run_in_threadpool
2. delete + restore advance the workspace to G+2
3. the retained context is restamped G+2 for the next turn
4. the blocked worker wakes, reads ctx.fence_stamp, and submits its effect as G+2
```

The stale actor's write is then perfectly fenced — against the wrong generation.
The worker-thread shape is real: `tools/references.py:527` uses
`run_in_threadpool`, and a Python thread cannot be interrupted.

So dispatch builds an immutable `Invocation` carrying the stamp and the
authority-bearing provenance, and the tool receives that. **The production
signature is exact and F3a states it:** `_safe_tool` currently calls
`impl(*args, **kwargs)` while its callers pass the mutable `ctx`. F3a changes that
call to construct the `Invocation` at dispatch and pass it in `ctx`'s place, and
**the overlapping-invocation test must drive that real dispatcher** — a test that
snapshots at the helper level passes while production still hands over mutable
context. A mutable context is a
belief that can be overwritten while someone is still acting on it.

`active_turn_job_id` cannot reuse `origin_job_id`: `registry.py:87` leaves that
field `None` for interactive contexts *by design*, and `tools/proposal.py:169`
copies it into durable provenance, so populating it would change stored data.

## 4. The gate

> **`tools/` may import only from a positive allowlist: the query, effect and
> accounting ports, their DTOs and commands, and the standard library. Not
> SQLAlchemy, not `db.models`, not `Session`, `Engine` or a session factory — and
> not any project module outside the allowlist.**

A forbidden-name list is not enough. A tool can reach an effectful project helper
that exposes no SQLAlchemy in the tool's own module, so the rule is stated
positively: anything not on the list is a violation, and adding to the list is a
reviewed diff.

### 4.1 The boundary must also remove resource locators

Closing database imports alone does not close semantic authority. `ToolContext`
exposes `repo_dir` (`registry.py:42`), so this passes every import fixture:

```python
(ctx.repo_dir / "proposal.md").write_text(new_text)
```

And `tools/history.py:63` legitimately imports `subprocess` for read-only git.

So: **`repo_dir`, `workspaces_dir` and every other resource locator leave the
tool-facing context**, and filesystem and git *reads* go behind query ports too.
A tool holds names of operations, not paths.

### 4.2 What this boundary is, and what it is not

It is **architectural hygiene for trusted code**: it makes the wrong thing hard to
write and easy to spot in review, and it makes coverage a CI check rather than a
memory. It is **not adversarial containment** — that would need a process, OS or
database-privilege boundary, and this design does not claim one.

Revision 5 claimed "a tool that cannot name a table cannot write to one." That is
false in one specific way worth keeping in view: a query port that accidentally
returns an **attached** ORM instance hands the tool `object_session(instance)`
without the tool importing anything. So the DTO rule needs its own contract test —
**every query result is recursively free of ORM instances** — not just a naming
convention.

### 4.3 Port construction

Ports are an injected bundle, built by the adapter factories and satisfying a
`Protocol`, with implementations in a package `tools/` may not import. **Not a
module global** — `effects._session_factory` would be reachable from any module
that can import `effects`, which is every tool.

**`ToolContext` stops carrying `session_factory` altogether**, so revisions 1–4's
privatisation problem disappears rather than being solved.

**Adding a new effect requires a reviewed implementation, a declared mechanism
(§3.5), and a straddle test (§9).** The cost falls on adding authority, not on
using it.

### 4.4 The F2 inventory becomes a checklist

`tests/fencing/` was built to *prove* coverage by analysis. Under an import
boundary it does not need to, and cannot usefully: the proof is the import check.

The inventory is retained as a **migration checklist** — the enumerated list of
sites still to convert, with `SLICE_STATUS` tracking which sub-slice owns each —
and its analysis machinery (`verifiers.py`, `_is_effect`, the containment rules) is
deleted in F3c. This is the demotion `2026-08-17` §9.1 already recorded; F3c
completes it.

## 5. Reads are not fenced

A read takes no lock and checks no generation.

**Rationale.** A stale read is not a durable effect. A stale read that *informs* a
durable write is caught by that write's operation, which re-checks the generation
inside its own transaction — the only moment that matters. Read revocation for
long-lived actors is a separate mechanism, settled in `2026-08-17` §6: rooms hold a
lease with room-enforced expiry.

**Consequence, stated rather than engineered around.** A tool can read a workspace
deleted microseconds later and show the agent data from a workspace that no longer
exists. The agent's *write* will fail. Bytes already delivered to the model are not
recoverable, exactly as `2026-08-17` §6 states for delivered socket bytes.

## 6. Behaviour on a stale generation

`WorkspaceGenerationStale` (F1) propagates out of the effect operation. The tool
layer converts it into a tool error the agent sees, and the turn continues; the
turn's existing terminal-state handling already covers a workspace that went away
underneath a running turn (Slice A, `7b16547`).

**This is design intent, not a verified claim.** The exact conversion point in the
tool-error path is confirmed against the code during planning, and the plan names
the test that pins it.

## 7. Scope — six sub-slices of F3

**The numbering does not move.** `2026-08-17` §11 assigns F4–F9 distinct meanings
(background job families, setup, turn jobs, collab writes, durable read checks, the
room lease), and the merged gate encodes them: `tests/fencing/inventory.py:20`
slices every `claim_fn` as `F4` and everything else as `F3`. These are **sub-slices
of F3**; F4–F9 are untouched.

### F3a — claim identity for every tool-originating actor *(additive)*

Nothing can be fenced until each actor that calls a tool knows which generation it
believes in. Today none does, and F1's `claimed_at_generation` column — added to
seven tables — is still unwritten.

Five actors build a `ToolContext` and therefore originate tool calls:

| Actor | Claim | Context |
|---|---|---|
| Interactive drafting | `turns/processor.py:215` (inline, not an inventory row) | `drafting/factory.py:92`, retained |
| Coherence | `jobs.py::claim_next_coherence_job` | `coherence/agent.py:42`, one-shot |
| Evaluator | `jobs.py::claim_next_evaluator_job` | `evaluator/agent.py:41`, one-shot |
| Review | `jobs.py::claim_next_review_run` | `review/agent.py:37`, one-shot |
| Uncited claims | `jobs.py::claim_next_uncited_claims_job` | `uncited_claims/agent.py:37`, one-shot |

F3a covers exactly these. **`queue.py::claim_queued_jobs` (analysis) and
`setup.py::claim_next_setup_job` build no `ToolContext` and originate no tool
call**, so they stay with their architecture slices — F4 and F5 respectively.
Revision 4's migration table wrongly pulled all six `claim_fn` rows into F3a.

Each claim sets **and returns** `claimed_at_generation` in the same statement.
One-shot contexts carry it at construction, which F2's inventory already records as
required for `ctx_lifetime: one_shot`. `set_turn_provenance`
(`drafting/claude_adapter.py:725`) gains the generation and `active_turn_job_id`,
stamped unconditionally every turn.

**The generation must come from the claim statement, never a read after it.**
Reading it afterwards reopens the exact race the fence closes: delete → restore
lands between claim and read, and the old actor adopts G+1 as its own belief.

**F3a owns the F2 gate migration, and cannot land without it.** The merged gate has
two *unconditional* inertness guards — `test_nothing_imports_the_fencing_primitives_yet`
(`test_fence_coverage.py:169`) and `test_nothing_reads_or_writes_claimed_at_generation_yet`
(`:191`). The moment a claim writes `claimed_at_generation`, the second goes red. So
F3a must, in the same slice:

- split `SLICE_STATUS` into `F3a…F3f` with `F3d` cut into `F3d1`, `F3d2`, `F3d3`, and reassign row ownership as follows —
  of the **6** `claim_fn` rows: the 4 tool-originating background claims → **F3a**,
  `queue.py::claim_queued_jobs` → **F4**, `setup.py::claim_next_setup_job` → **F5**; 5 × `tool_context` → **F3a**;
  5 × `tool_commit` (submitter and read operations) → **F3d2**: `submit_review_impl`, `submit_scorecard_impl`, `submit_uncited_claims_impl`, `fetch_reference_impl`, `fetch_reference_impl._refund_fetch_slot`;
  2 × `tool_commit` (the propose operations, DB-only) → **F3d3**: `propose_citation_impl`, `_propose_artifact_edit_impl`;
  `_do_fetch_and_ingest` and `_write_citekey_cache` → **F3e**;

  **Revised 2026-08-21 (F3d3b).** Revision 8 put `_write_citekey_cache` in F3d3 with the
  two propose sites. That allocation is wrong, and F3d3b's revision 0 proved it by
  building against it: the row's only owner-transaction caller is
  `_insert_citation_locked`, whose session holds the workspace commit mutex EXCLUSIVELY
  and has not committed when the cache write runs. A mechanism-1 operation reaches it on
  a SECOND connection and asks for the shared lease on the same key, so it blocks for
  `lock_timeout` and then raises — reproduced against a real `pgvector:pg16` container,
  and warned about in `port_impl/effects.py`'s own module docstring. §3.5 already says an
  effect spanning DB and git needs an operation owning the entire sequence under the
  mutex, and §3.5's mechanism rule — and `EFFECT_MECHANISMS`, which encodes it — says the exclusive-mutex mechanism arrives in F3e. So
  the row belongs with the mechanism that can fence it. F3d3 keeps exactly the two rows
  that share mechanism 1.
- replace both inertness guards with **scoped per-sub-slice obligations** — "nothing
  outside F3a's identities writes the column yet" — rather than "nothing does";
- leave `queue.py::claim_queued_jobs` and `setup.py::claim_next_setup_job` in F4/F5.

F3c then installs a **ratchet** against the violations that still exist — the
**exact checked-in set** of remaining violating identities, not a count. A count
lets one old violation disappear while a new one is introduced, and **F3f** brings it to zero and activates
the permanent boundary. A gate that must pass green at every sub-slice boundary is
otherwise impossible: F3d cannot satisfy a live import rule while ingest and the
capability holders deliberately remain.

**The claim generation needs plumbing that does not exist.** The drains discard
everything but the job: `coherence/pass_runner.py:189` is
`job = claim_next_coherence_job(s); s.commit()` and returns `None`. F3a threads the
returned generation through each `drain_* → run_* → adapter_builder` chain.

F3a also lands the runtime two-turn test F2 owes: two turns through one retained
context across a generation change, the second carrying the new value — **plus the
overlapping-invocation test**, which is the one that matters: block a tool
invocation at G, restamp the retained context to G+2, release the blocked
invocation, and prove it still presents G (§3.4).

### F3b — the ports *(additive; no caller converted)*

`ports/queries.py`, `ports/effects.py`, `ports/accounting.py`, the DTOs and
commands, `FenceStamp`, the shared-lease helper of §3.5, and the exclusive
`acquire_commit_mutex_at_generation` from F1. **There is no
`require_live_generation`** — §3.5 explains why a bare pre-check cannot exist.

**F3b ships a table naming every effect operation and the mechanism it uses**, and
each operation arrives with the two ordered lock cases for *that* mechanism (§9).

**F3b also owns correcting three surfaces that currently document the predicate-only
form as correct**, and their tests. No production caller uses them, so this is
documentation and test debt rather than a behaviour change — but all three read as
authoritative and would be followed:

- `2026-08-17` §3.1, which presents predicate-only as the default and states it adds
  no lock;
- `live_at_generation`'s docstring (`fencing.py:67`), which shows exactly that usage;
- `acquire_commit_mutex_at_generation`'s docstring, which recommends the predicate
  for DB-only effects. No tool changes, so the branch
is inert in the F1 sense.

### F3c — the gate, and the inventory demotion

The import boundary of §4, with negative fixtures. `verifiers.py` and the
containment machinery are deleted; the inventory is reduced to a checklist (§4.2).
Still nothing converted, so the gate is proven against fixtures before it has live
data.

### F3d — convert the tool sites

**This is a module-level dependency migration, not five site conversions.** Moving
only the inventory identities leaves `tools/citations.py` importing SQLAlchemy and
models, so the boundary stays red. Other session/model-dependent helpers live there
and are consumed by analysis, human-cite, collab and accepting:
`_resolve_citekey_for_ref` takes a `Session` and `Reference` (`citations.py:250`),
`compute_citation_insert` takes a session, queries models and returns an **attached**
`Reference` (`:567`), and the unregistered `insert_citation_impl` obtains a session.

So F3d moves the whole session/model-dependent citation core out of `tools/` into
the effects/domain layer and updates **every non-tool consumer** — then converts
`propose_citation_impl`, `_propose_artifact_edit_impl`, `submit_review_impl`,
`submit_scorecard_impl` and `submit_uncited_claims_impl` to effect operations, the
two `references.py` accounting sites to owner-bound operations, and every remaining
read to a query port returning DTOs.

**Revised 2026-08-21 (F3d3b).** Revision 8 said "`caller_fenced` and the `accepting.py`
injection retire with the move". The move happened in F3d3a and they did not. The one
`caller_fenced` row, `_write_citekey_cache`, is fenced by `_insert_citation_locked`, which
holds the commit mutex EXCLUSIVELY — so the category cannot retire until an operation owns
that whole sequence, which is F3e's (see §7's allocation note). What the move DID retire is
the reason the category was unavoidable: the code is now outside `tools/`, so its fencing
is an ordinary mechanism question rather than a tool-boundary exemption.

The attached-instance requirement in `compute_citation_insert` is legitimate — it
just belongs *inside* the relocated core, never across the tool boundary.

### F3e — the ingest pipeline, and the mechanism-2 effects

**Revised 2026-08-22 (F3e1a Task 4).** F3e is split into three sub-slices for the same
reason F3d was: SLICE_STATUS has no partial state, so a multi-slice conversion needs one
key per slice or the first flipped row goes red. The three keys are `F3e1a` (complete —
the carrier, the git detector, the gates; no inventory row of its own), `F3e1b` (pending
— the git-effect conversions), and `F3e2` (pending — the ingest pipeline and the
filesystem effects). The mechanism-2 effects (including `_write_citekey_cache`) are fenced
by **converting the mutex acquisition**, not by a port operation; the argument is in
`2026-08-21-fence-f3e1-git-mechanism-design.md` §9.

F3e is where the **exclusive-mutex mechanism** arrives (§3.5's rule, encoded in
`EFFECT_MECHANISMS`),
so it owns every effect that spans the DB and either git or the filesystem — not only
ingest. Two rows sit here:

- `fetch_reference_impl._do_fetch_and_ingest` — the ingest pipeline, described below.
- `_write_citekey_cache` — reassigned from F3d3 on 2026-08-21; see §7's revision note.
  Its operation is not a cache-write operation. The fenceable unit is the whole
  accept-time sequence in `_insert_citation_locked`: mutex, then one git commit of
  `proposal.md` + `citations.json`, then the rows. §3.5 names this function as its
  example. Its SAVEPOINT callers (`accepting.py`, `collab/kernel.py` ×2) are inside
  transactions they commit themselves and are fenced by those callers.

Both share the shape F3e must solve: **bytes land before rows**, so a proof that only
inspects rows passes while a stale generation has already written to disk.

`references/ingest.py` runs four phases in **deliberately** separate transactions:
phase 1 commits the `Reference` row on the caller's session (`:389`), then text
extraction and embedding run — slow — and later phases commit status transitions on
fresh sessions (`:121`, `:188`, `:202`, `:219`, `:297`, `:318`).

**The fresh sessions are correct and must survive.** One transaction across
embedding would pin the commit mutex for its duration — the failure `2026-08-17` §5
exists to prevent, and why `2026-08-17` §10 says *fence what is frequent, barrier what
is destructive*.

**Ingest also writes the filesystem before its rows.** Original bytes land at
`ingest.py:360` before the `Reference` row commits, and extracted text before its
status commit. F3e's operations and their proofs must name those filesystem effects
explicitly — a proof that only inspects rows would pass while bytes were written on
a stale generation.

So ingest becomes **phased, idempotent operations**, each re-checking at its own
boundary, and F3e answers a question no earlier sub-slice raises: **what does a
stale generation mean for a half-ingested reference?** The row exists, text may be
extracted, embeddings may be partial. Abandoning without writing is prohibited by
`2026-08-17` §7 — it strands rows in `running`. The likely answer is a terminal status
transition that is itself an accounting operation, but that is F3e's design work.

### F3f — capability holders, then the handle

`tools/audit.py` and the citation-audit write path receive a `VerdictCache` port.

`citation_service` needs **more than a `UsageRecorder`**: it uses its factory for
two capabilities, not one — it *reads* caps via `check_citation_caps`
(`service.py:73`) as well as recording usage (`:114`). A write-only recorder cannot
preserve `complete_json`. It receives either a `CitationUsageStore` with
`check_caps` and `record`, or a separate `CapReader` and `UsageRecorder`. **`session_factory` is removed from
`ToolContext` in this sub-slice's final commit**, once nothing reaches for it.

## 8. What this design does not do

- **Does not fence reads** (§5).
- **Does not renumber F4–F9** (§7).
- **Does not offer a generic fenced transaction** (§3.2). A caller-filled
  transaction cannot be correct across DB and git.
- **Does not keep the transaction token as a correctness mechanism.** It is
  retained as **diagnostics and telemetry** inside the effect operations, where it
  is genuinely useful for catching internal bugs. It proves nothing about a caller.
- **Does not touch the job families, setup, turn finalizers, or collab rooms** —
  `2026-08-17` F4–F9, unchanged.
- **Does not remove any compensation.** Step 1 of fence → prove → remove → relax
  stays purely additive. The 6s barriers, `reconcile_cancelled_jobs` and
  cancel-on-delete all stay until step 3.

## 9. How each sub-slice is proven

**The claim generation is proven by the race it closes** (F3a). Interleave
delete → restore between the claim and the first tool write, and assert the actor
still believes its claimed generation rather than the current one. A test that
reads the generation after the claim passes whether or not the fix is present, so
it must drive through the claim statement itself.

**The retained context is proven at runtime** (F3a). Two turns through one context
across a generation change; the second must carry the new value. No static check
can observe a per-turn re-stamp, and `registry.py:80` documents the identical trap
for provenance, where a stamp missed once leaks the previous turn's value.

**Every effect operation is proven by TWO ORDERED LOCK CASES** (F3b, F3d–F3f).

Revision 7 prescribed a straddle: pause the effect mid-flight, commit a
delete/restore in the gap. **That test deadlocks a correct implementation** — the
paused effect holds the shared lease, delete needs the exclusive lock, and each
waits for the other. A test that can only pass against a broken build is worse than
no test, and this one was written into the spec as the primary proof.

The lease protocol is proven by which side wins the lock, both directions:

```python
# CASE A — effect acquires shared first
#   lifecycle blocks until the effect commits, then transitions.
#   The effect's write LANDS, and that is correct: it was live when it ran.

# CASE B — lifecycle acquires exclusive first
#   the effect blocks; after the transition it acquires shared, evaluates its
#   predicate against the NEW generation, and lands NOTHING.
assert_effect_did_not_land()
```

Case B is the one that matters, and both must be asserted — Case A proves the fence
does not simply refuse everything.

**The inside-SQL pause moves to a predicate-only regression test.** It is no longer
a proof of the operation; it is the demonstration of *what happens when the shared
lease is removed*, and it belongs with the mutation that removes it. Written as an
`INSERT`, which has no target row and therefore no READ COMMITTED re-check to mask
the race — the shape reproduced on PostgreSQL 16.

**And the assertion must name the field, not the row count.** Three of the five
semantic sites *update an existing row* — `scorecard.py:92` writes a JSONB field,
`review_submit.py:19` writes `raw_response_json`, `uncited_claims.py:43` writes
`claims`. A stale write lands with the row count unchanged, so revision 5's stated
bar would have passed a broken implementation. Compare the affected fields, or a
full row snapshot.

**For git and filesystem effects, an unchanged HEAD is not enough** — a stale
implementation can dirty the working tree without committing. Assert HEAD,
worktree cleanliness and content, and the relevant storage paths (§7, F3e).

**The compensation must be disabled for each per-site test** (F3d–F3f), per
`2026-08-17` §9: a conversion test that passes with the fence removed is measuring
the compensation, not the fence. This is the technique that exposed C1's decorative
tests.

**Accounting is proven by the interleaving it exists for** (F3d): reserve a slot at
generation G, delete and restore, start a new turn at G+1, and assert the old
actor's refund does not touch the new turn's row.

**The gate is proven against the ALLOWLIST, not a blacklist** (F3c). Fixtures that
only import `Session`, `sessionmaker`, `Engine`, a model or `sqlalchemy` would pass
against a forbidden-name implementation, which is not the rule §4 states. So the
decisive fixture imports a **plausible but unapproved internal helper** — something
with no SQLAlchemy in its own module — and must fail. Plus the recursive DTO
contract test of §4.2, and a fixture reaching a resource locator
(`ctx.repo_dir`) once locators are removed.

## 10. Risks

- **A fenced operation can hold the mutex for ~180 seconds.** `citations/gitio.py:21`
  documents its own worst case: `write_and_commit_files` runs up to four git
  subprocesses serially plus one in rollback, so *"gitio can hold the commit mutex
  for ~180s in the worst case."* Any effect operation that wraps a git write
  inherits that. Slice C3's 2s `lock_timeout` cap does **not** bound it — the cap is
  installed *after* acquisition, and initial acquisition defaults to 10s
  (`drafting/locking.py:205`). Multi-resource operations (§3.2) must treat this as a
  known cost, and F3d should measure it rather than assume it.
- **Not every write currently takes the mutex, and one is not even in `tools/`.**
  `review/pass_runner.py:344` writes git with zero mutex acquisitions. The import
  boundary does not reach it, because it is not a tool — it is a pass runner. It
  needs an effect operation of its own, and `2026-08-17` F4 is where that lands.
  **Its operation must take the mutex BEFORE loading the locked `ReviewRun` row** —
  wrapping the present block as-is creates the J→M inversion `2026-08-17` §5 names.
- **The ceremony must stay affordable.** Every new effect needs an operation, a
  command, a DTO and a test. If that becomes onerous, the pressure will be to add a
  generic escape — `effects.execute(stmt)` — which reinstates ambient authority
  under a new name. There is no acceptable version of that method.
- **DTO drift.** Query ports return frozen copies, so a field the tool needs but
  the DTO omits is a port change, not a `.` away. Expect several rounds of adding
  fields in F3d, and prefer widening a DTO over returning an attached instance.
- **The sub-slices must land in order, and F3f must land.** If F3d–F3e ship and F3f
  never does, `session_factory` stays on `ToolContext` and the boundary is
  convention. An unfinished sequence is the most likely way this design decays.
- **The import gate does not cover the effects layer itself.** Operations legitimately
  import everything. Their correctness rests on review and the stale-generation
  test per operation, which is why §4 makes adding one deliberately expensive.
- **Ingest and multi-resource effects remain the hard cases.** F3e's half-ingest
  semantics and the DB-plus-git ordering in `_insert_citation_locked` are real
  design work that no boundary dissolves.

## 11. Changelog

**Revision 8 (2026-08-18).** An eighth review independently reproduced all three
lock states on PostgreSQL 16 and confirmed the protocol. Five defects remained, and
the first was in the proof rather than the design.

- **The prescribed straddle test deadlocks a CORRECT implementation.** It paused the
  effect while it held the shared lease and then waited for delete/restore to
  commit — but delete needs the exclusive lock, which the paused effect is holding.
  A test that can only pass against a broken build was standing as this design's
  primary proof. Replaced by two ordered lock cases: effect-first (blocks lifecycle,
  write lands, correctly) and lifecycle-first (blocks effect, predicate then sees
  the new generation, nothing lands). The inside-SQL pause survives only as the
  regression test showing what the removal of the shared lease costs (§9).
- **The canonical example still had no lease.** §3.5 was corrected in revision 7 and
  §2's headline block was not, so the design's most-read code was, again, exactly the
  implementation the surrounding text rejects (§2).
- **"Every effect takes a shared lease" contradicted the exclusive mechanism.** Taken
  literally, a multi-resource operation would take shared and then upgrade — two such
  operations deadlock. Each operation now selects exactly one path and never
  upgrades; git and filesystem work takes the exclusive lock first (§3.5).
- **F3b now owns correcting three authoritative-but-wrong surfaces**: `2026-08-17`
  §3.1, `live_at_generation`'s docstring, and
  `acquire_commit_mutex_at_generation`'s. All present predicate-only as correct, and
  all would be followed. No production caller, so it is documentation and test debt.
- The migration table said "6 × claim_fn remainder" when 6 is the total (4 → F3a,
  1 → F4, 1 → F5), and a second §4.2 heading remained. Both fixed.

**Revision 7 (2026-08-18).** A seventh review kept the architecture and found one
fundamental concurrency defect plus five corrections.

- **Primitive A does not serialize against a lifecycle change.**
  `live_at_generation` is an `exists()` subquery that takes **no lock on the
  workspace row**, so under READ COMMITTED it reads the statement's snapshot and a
  delete/restore committing after it is invisible. Reproduced on PostgreSQL 16 with
  an `INSERT … SELECT … WHERE live_at_generation(...)` paused inside the statement.
  For `UPDATE` this is masked only because cancel-on-delete touches the same target
  row and triggers PostgreSQL's re-check — **compensation the architecture removes
  in step 3**, so the masking expires. Every effect now takes
  `pg_advisory_xact_lock_shared` on the workspace mutex key, against which
  lifecycle transitions hold the existing exclusive lock; effects stay concurrent
  with each other. The predicate remains, because it is what turns "I waited" into
  "I checked" (§3.5).

  **This is a defect in merged F1, not only in this spec.** F1 is inert — no
  production caller — so nothing is broken today, and the fix lands as a companion
  primitive in F3b rather than as a change to `fencing.py`'s existing surface.
  `jobs_retirement._still_at_generation` has the same shape but a different failure
  mode: a missed *cancel*, which the reaper catches, not a stale semantic effect.

- **Mechanism 3 was too wide.** Revision 6 allowed asserting on any already-locked
  target row. An `EvaluatorJob` or `UncitedClaimsJob` carries no lifecycle state, so
  locking one proves nothing about the generation — and what makes it look safe is
  once more cancel-on-delete. Restricted to the `Workspace` row, as `2026-08-17`
  §3.1 always said.

- **F3b still specified `require_live_generation`**, which §3.5 and revision 6's own
  changelog say cannot exist. Removed, and F3b now ships the per-operation mechanism
  table §3.5 requires.

- **Two gate proofs were decorative.** A count-only ratchet lets one violation be
  swapped for another — it now pins the exact set. And the negative fixtures tested
  forbidden names, so a blacklist implementation would pass every one; the decisive
  fixture now imports a plausible *unapproved internal helper*.

- **The straddle test must pause inside the SQL**, after the predicate qualifies. A
  Python pause before `execute()` cannot expose an MVCC-snapshot race. The `INSERT`
  case is written first, being the one with no re-check to mask it.

- **The dispatcher signature is now exact.** `_safe_tool` calls
  `impl(*args, **kwargs)` with the mutable `ctx`; F3a changes that call site, and
  the overlapping-invocation test must drive the real dispatcher.

Also: the header said revision 5, §4.1/§4.2 were duplicated, `origin_job_id` was
explained twice, and the F3a migration table was referenced but missing. All fixed.

**Revision 6 (2026-08-18).** A sixth review kept the architecture and found seven
targeted defects. Its verdict — *"another wholesale redesign is not warranted"* — is
why this is a revision and not a rewrite.

- **The headline example was the two-statement race.** It checked the generation and
  then wrote; under READ COMMITTED a delete/restore fits between the statements.
  That is F1's Global Constraint 3 and the defect that shipped here twice
  (`28abbc4`, `701fbb7`), written into this design's most-read code block. There is
  now a table of four concrete atomic mechanisms, each operation declares which one
  it uses, and the invented `require_live_generation` is gone (§3.5).
- **The retained stamp was mutable across invocations.** `set_turn_provenance`
  restamps the retained context every turn, so a tool blocked in
  `run_in_threadpool` wakes and reads a *newer* generation — its stale write then
  fences perfectly against the wrong epoch. The stamp is now snapshotted per
  invocation at dispatch, and the overlapping-invocation test is the proof (§3.4).
- **The import boundary was not closed.** `ctx.repo_dir` lets a tool write
  `proposal.md` directly while passing every import fixture, and `tools/history.py`
  legitimately imports `subprocess`. The rule is now a positive allowlist, resource
  locators leave the tool-facing context, and the claim is stated honestly:
  architectural hygiene for trusted code, not adversarial containment (§4).
- **F3a–F3c could not land green.** F2's two *unconditional* inertness guards go red
  the moment a claim writes the column or a port imports the primitives. F3a now
  owns the gate migration, F3c installs a ratchet, F3f reaches zero (§7).
- **F3d was a module migration, not five conversions.** `tools/citations.py` holds
  other session/model-dependent helpers consumed by four non-tool paths; moving two
  identities leaves the module importing SQLAlchemy (§7).
- **A `UsageRecorder` was insufficient** — the citation service also *reads* caps at
  `service.py:73`. And port construction is now specified as an injected bundle,
  since a module global would make `effects._session_factory` reachable from every
  tool (§4.3, §7).
- **Two proofs were decorative.** A stale-stamp call proves only that a pre-check
  exists; the tests must straddle. And row counts are unchanged for the three sites
  that *update* a JSONB field, so the assertion must name the field (§9).

Also corrected: eight directly-imported model classes, not ten; ingest writes
original bytes at `ingest.py:360` before its row commit, so F3e's proofs must cover
filesystem effects; and `pass_runner`'s eventual operation must take the mutex
before loading its locked row, or it creates a J→M inversion.


**Revision 5 (2026-08-18).** Redesigned after a fifth review. Revisions 1–4 each
tried to hand tool code a `Session` while guaranteeing its transaction could not be
misused, and each was defeated empirically. Revision 4's token was the clearest
failure of the four: it detects an escape *after* the effect is durable, and an
exception cannot retract a row, a git commit or a file — so it satisfies
"fail loudly", not the architecture's "no stale semantic effect."

The diagnosis was that the abstraction boundary, not the wrapper, was wrong. A
`Session` is ambient authority — transactions, connections, engines, raw SQL, ORM
back-references, events — so there is always another escape. Tools now receive
narrow query, effect and accounting ports and never a handle, each operation owning
its own predicate, transaction and commit. The coverage gate collapses to an import
check, which is the first closed rule this design has had.

Also corrected: revision 4's migration pulled all six `claim_fn` rows into F3a, but
`queue.py::claim_queued_jobs` and `setup.py::claim_next_setup_job` originate no tool
call and stay with F4 and F5. And a defect present in all four earlier revisions
and caught by none of the first three reviews — **a database transaction cannot roll
back git**, so every `fenced_write` promised an atomicity it could not deliver.

Retained unchanged: F1's primitives, the generation model, claim identity,
owner-bound accounting, and F3a. The transaction token survives as diagnostics
inside the operations, never as proof.
