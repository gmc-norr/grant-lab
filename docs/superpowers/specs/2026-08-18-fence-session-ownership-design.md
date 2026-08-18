# Fence F3 — Ports, not Handles

**Status:** agreed 2026-08-18 (revision 5). Mechanism spec for sub-slices F3a–F3f.

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
effects.submit_scorecard(ctx.fence_stamp, SubmitScorecard(...))

# In the operation — fixed, reviewed, and the caller never sees this.
def submit_scorecard(stamp: FenceStamp, cmd: SubmitScorecard) -> None:
    with _session_factory.begin() as session:
        require_live_generation(session, stamp)     # F1's primitive
        session.add(Scorecard(...))
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

### 3.4 `FenceStamp`

A frozen `(workspace_id, generation)` pair, plus `active_turn_job_id` for
accounting. It is the actor's belief, captured at claim time and never refreshed.

**It cannot reuse `origin_job_id`.** `registry.py:87` leaves that field `None` for
interactive contexts *by design*, and `tools/proposal.py:169` copies it into durable
provenance — populating it would silently change stored data.

## 4. The gate

> **`tools/` may not import SQLAlchemy, `db.models`, `Session`, `Engine`, or a
> session factory. It may import only the approved query, effect and accounting
> ports and their DTOs and commands.**

That is the whole rule. It is an import check: closed, decidable, and one grep
wide. A tool that cannot name a table cannot write to one.

Everything F2's verifier struggled with — the open effect set, the acquisition
set, containment, `caller_fenced` — was downstream of tools holding a handle they
should not have had. None of it survives the boundary.

**`ToolContext` stops carrying `session_factory` altogether**, so revisions 1–4's
privatisation problem disappears rather than being solved: there is no attribute to
make private, no `getattr` spelling to police, and no `AttributeError`-in-production
staging hazard.

**Adding a new effect requires a reviewed implementation and a stale-generation
test.** That is the point of the ceremony: the cost falls on adding authority, not
on using it.

### 4.1 What the boundary also fixes

- **Injected sessions.** `proposals/accepting.py:207` passes its own request-owned
  session into `_insert_citation_locked` under `tools/`. That helper moves into the
  effects layer, where both callers reach it as an operation. `caller_fenced`
  disappears as a category.
- **Capability holders.** `ctx.citation_service` owns a session factory and commits
  usage records (`citations/service.py:114`); `tools/audit.py:90` hands the factory
  to the audit engine, which reaches `citations/verdict_cache.py::store_verdict`.
  Each receives a narrow port instead — a `UsageRecorder` and a `VerdictCache` —
  rather than a factory.

### 4.2 The F2 inventory becomes a checklist

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

F3a also lands the runtime two-turn test F2 owes: two turns through one retained
context across a generation change, the second carrying the new value.

### F3b — the ports *(additive; no caller converted)*

`ports/queries.py`, `ports/effects.py`, `ports/accounting.py`, the DTOs and
commands, `FenceStamp`, and `require_live_generation` over F1's primitives. Each
operation gets its stale-generation test on arrival. No tool changes, so the branch
is inert in the F1 sense.

### F3c — the gate, and the inventory demotion

The import boundary of §4, with negative fixtures. `verifiers.py` and the
containment machinery are deleted; the inventory is reduced to a checklist (§4.2).
Still nothing converted, so the gate is proven against fixtures before it has live
data.

### F3d — convert the tool sites

`propose_citation_impl`, `_propose_artifact_edit_impl`, `submit_review_impl`,
`submit_scorecard_impl`, `submit_uncited_claims_impl` to effect operations; the two
`references.py` accounting sites to owner-bound operations; `_write_citekey_cache`
and `_insert_citation_locked` into the effects layer, which retires `caller_fenced`
and the `accepting.py` injection with them. Every read under `tools/` becomes a
query port returning DTOs.

### F3e — the ingest pipeline

`references/ingest.py` runs four phases in **deliberately** separate transactions:
phase 1 commits the `Reference` row on the caller's session (`:389`), then text
extraction and embedding run — slow — and later phases commit status transitions on
fresh sessions (`:121`, `:188`, `:202`, `:219`, `:297`, `:318`).

**The fresh sessions are correct and must survive.** One transaction across
embedding would pin the commit mutex for its duration — the failure `2026-08-17` §5
exists to prevent, and why `2026-08-17` §10 says *fence what is frequent, barrier what
is destructive*.

So ingest becomes **phased, idempotent operations**, each re-checking at its own
boundary, and F3e answers a question no earlier sub-slice raises: **what does a
stale generation mean for a half-ingested reference?** The row exists, text may be
extracted, embeddings may be partial. Abandoning without writing is prohibited by
`2026-08-17` §7 — it strands rows in `running`. The likely answer is a terminal status
transition that is itself an accounting operation, but that is F3e's design work.

### F3f — capability holders, then the handle

`tools/audit.py` and the citation-audit write path receive a `VerdictCache` port;
`citation_service` receives a `UsageRecorder`. **`session_factory` is removed from
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

**Every effect operation is proven by the effect that must not land** (F3b, F3d–F3f)
— not by an exception type:

```python
op_under_test(stale_stamp, command)          # raises, or does not
assert count_of(Model) == before             # THE assertion
assert git_head(repo) == head_before
```

Revisions 1–4 proposed tests asserting that *some exception eventually occurred*.
That is satisfied by a design which commits and then complains. The assertion is
that nothing landed.

**The compensation must be disabled for each per-site test** (F3d–F3f), per
`2026-08-17` §9: a conversion test that passes with the fence removed is measuring
the compensation, not the fence. This is the technique that exposed C1's decorative
tests.

**Accounting is proven by the interleaving it exists for** (F3d): reserve a slot at
generation G, delete and restore, start a new turn at G+1, and assert the old
actor's refund does not touch the new turn's row.

**The gate is proven by negative fixtures** (F3c): a module under `tools/`
importing `Session`, `sessionmaker`, `Engine`, a model, or `sqlalchemy` at all must
fail, each as its own fixture.

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
