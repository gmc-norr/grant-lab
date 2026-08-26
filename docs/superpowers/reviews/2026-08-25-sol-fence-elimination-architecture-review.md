# SOL review: eliminating the workspace lifecycle fence

**Date:** 2026-08-25  
**Status:** Architecture recommendation  
**Scope:** Structural alternatives to the current generation fence for workspace disable, restore, and purge  
**Repository baseline:** `8ea3f68e`

## Executive decision

There is an architecture that removes the lifecycle fence, but neither a new LLM
manager nor moving git history into PostgreSQL is sufficient by itself.

The fence exists because three facts currently coincide:

1. A stable workspace identity is reused after restore.
2. Long-lived actors retain direct mutation authority: database sessions,
   repository paths, storage handles, or helpers that expose them.
3. Those actors may perform an effect long after the authorization decision that
   admitted them.

To remove the fence, change the first two facts:

- Keep the stable public workspace ID, but make each active lifetime a distinct,
  never-reused **workspace incarnation**.
- Put every workspace-scoped semantic mutation behind one **workspace state
  service**, with one short-command mailbox per incarnation.
- Give HTTP handlers, WebSocket connections, schedulers, and LLM workers no
  direct mutation authority. They submit commands or immutable candidates.
- Bind a WebSocket session or background job to its incarnation durably when the
  state service admits it. Later messages identify that session or job; the
  caller never supplies or re-mints an incarnation.
- Seal an incarnation permanently on owner delete. Restore creates a new
  incarnation from the sealed snapshot and atomically makes it current.

An old actor can then address only the old incarnation. That mailbox is closed
forever. It cannot discover or address the replacement incarnation, so a stale
writer is not a reachable state.

This is the actor-incarnation distinction described by Akka: a path may be reused,
but an actor reference identifies one lifetime, and a reference to a terminated
incarnation is never valid for a later incarnation at the same path:
<https://doc.akka.io/libraries/akka-core/current/general/addressing.html#what-is-the-difference-between-actor-reference-and-path>.

Use a separate local **Python** process and PostgreSQL role for the state service.
Go or Rust would not improve the correctness argument. The process and capability
boundary matter; the implementation language does not.

Moving document history from per-workspace git repositories to append-only
PostgreSQL versions is also recommended because it removes the remaining
cross-store transaction problem. It is complementary, not the lifecycle solution:
a PostgreSQL-only writer can still be stale across delete and restore if the same
identity is reused and it retains direct write authority.

## Judgment gate

### Governing principle

An invalidated authority must never become valid again merely because a logical
name is reused.

### Falsifying experiment

Pause an actor after admission but before its effect, delete the workspace,
restore it, then resume the actor.

The proposed architecture fails if either condition occurs:

- the old actor changes any state observable through the restored workspace; or
- delete must wait for the paused actor to finish.

### Already-refuted mechanisms

The repository has already refuted these as correctness mechanisms:

- enumerate and kill every actor;
- treat the commit mutex as proof that all work drained;
- rename the repository and repair selected stores later;
- infer fence provenance from increasingly complete Python AST analysis.

The rejected quiescence design records why threads, untracked writers, and
already-spawned git processes defeat kill-as-proof:
[2026-08-18-quiesce-and-seal-design.md](../specs/2026-08-18-quiesce-and-seal-design.md).

## What “eliminate” means

A design eliminates the fence only if all of the following disappear from the
steady-state correctness argument:

- generation/epoch equality at each effect;
- a token whose provenance must be threaded from authorization to effect;
- per-write liveness rechecks;
- static enumeration of every effect site;
- cancellation as proof that no stale effect can land.

A central function that compares an incarnation ID against
`current_incarnation_id` on every write is still a fence. It is better located,
but it has not eliminated the problem.

The proposed design retains one lifecycle decision when an incarnation is
activated or addressed: active incarnations have mailboxes; sealed incarnations
do not. That is object lifetime, not per-effect token validation. Crucially, an
old job or session cannot redirect itself to the current incarnation.

## Current architecture pressure

Measured at `8ea3f68e`:

| Surface | Measured size |
|---|---:|
| `tests/fencing/*.py` plus its inventory | 5,503 lines |
| fence, ports, and SQL port implementations | 1,381 lines |
| source files naming `acquire_commit_mutex_at_generation` | 17 |
| calls matching the principal git history helpers | 42 |

The current mechanism is correct. The unusual cost is proving that a stamp was
minted at the authorization boundary rather than beside the effect.
[stamp_gate.py](../../../tests/fencing/stamp_gate.py) explains the exact failure:
a perfectly shaped `stamp.generation` is useless when `stamp` was created from a
fresh read at the mutex.

Deleting the old trusting primitive was valuable, but it cannot delete this
provenance problem. Python types can express “this is a `FenceStamp`”; they cannot
prove “this value came from the earlier authorization decision.”

## Target architecture

### 1. Stable path, immutable incarnation

`Workspace.id` remains the stable product identity used in URLs, audit, ownership,
and admin surfaces. It points to at most one active incarnation.

```text
Workspace (stable logical identity)
  id
  current_incarnation_id -> WorkspaceIncarnation.id, nullable

WorkspaceIncarnation (one lifetime)
  id                     random UUID, never reused
  workspace_id
  state                  active | sealed | purged
  base_version_id
  head_version_id
  created_at
  sealed_at
```

The identity transition is:

```text
workspace W
  incarnation A: active -> sealed forever
  incarnation B: created from A.head_version -> active
```

There is no `sealed -> active` transition. Restore means creating B, not reviving
A.

### 2. One local state service

Run one additional local Python process, reached over a Unix-domain socket. It
hosts a lightweight mailbox per active incarnation; it is not one OS process per
workspace.

The service alone has:

- DML permission on workspace-scoped semantic tables;
- permission to append document versions and move the current head;
- writable access to workspace files if git is retained temporarily.

FastAPI, APScheduler callbacks, LLM subprocesses, and extraction workers do not
have those capabilities. Database roles and filesystem permissions enforce the
boundary. A Python package boundary inside the existing process is useful design,
but it does not make bypass impossible and would still require structural tests.

This is the part that makes the bug class unreachable. Without the authority
boundary, an incarnation UUID is only a larger generation token.

Cloudflare Durable Objects document the analogous combination of unique object
identity, single-threaded coordination, and storage accessible only within that
object:
<https://developers.cloudflare.com/durable-objects/concepts/what-are-durable-objects/>.
The application does not need Cloudflare; the pattern can be implemented locally.

### 3. Short commands only

The mailbox serializes state transitions and semantic writes, not computation.
No handler awaits an LLM, network fetch, user action, or long extraction.

```text
short command: authorize -> validate current state -> transact -> reply
long work:     snapshot -> compute elsewhere -> submit immutable candidate
```

This prevents a minutes-long LLM turn from blocking delete.

The `Seal` command defines the delete linearization point. Commands already ahead
of it finish before deletion. Commands behind it cannot enter the sealed mailbox.
That is a total order, not a race between a database transition and an external
writer.

### 4. Authority derives from durable admission

Callers must not submit `workspace_id + incarnation_id` to an unrestricted
“apply” endpoint. That recreates the provenance problem in another form.

Instead, the state service creates durable admissions:

```text
WorkspaceSession
  id
  incarnation_id
  kind                   websocket | llm_job | maintenance
  state                  active | closed

LLMJob
  id
  session_id
  input_snapshot_id
  state
```

The important direction is:

```text
result(job_id) -> job.session_id -> session.incarnation_id
```

not:

```text
result(workspace_id, incarnation_id supplied by worker)
```

The worker receives a job ID and immutable inputs. When it returns a candidate,
the state service derives the destination from the durable job record it created.
The worker cannot adopt a newer incarnation.

The same applies to WebSockets. A connection is admitted to one incarnation and
receives a session ID. Frames carry the session ID, not a fresh workspace lookup.
Sealing closes those sessions and their mailbox. A reconnected client is a new
admission and therefore belongs to the new incarnation.

For ordinary HTTP writes, authorization and mutation run in the same short mailbox
command. A request delayed until after restore is admitted after restore; it is
not a pre-delete admission. This makes the system linearizable without handing the
request a long-lived token.

### 5. Candidate-only LLM execution

The LLM executor is not a workspace manager and should not be a writer. It may:

- receive immutable context snapshots;
- call model providers;
- perform pure parsing and validation;
- return typed candidate commands and accounting facts.

It may not receive:

- a SQLAlchemy `Session` or session factory;
- a repository or workspace path;
- a writable storage adapter;
- a resolver from stable workspace ID to current incarnation;
- a generic callback capable of arbitrary domain mutation.

The existing command DTOs in
[ports/effects.py](../../../src/call_manager/proposal_assist/ports/effects.py)
and immutable invocation work in
[ports/invocation.py](../../../src/call_manager/proposal_assist/ports/invocation.py)
are useful migration assets. Their destination should become a job-bound mailbox,
not a caller-supplied `FenceStamp`.

[ToolContext](../../../src/call_manager/proposal_assist/tools/registry.py) is the
current counterexample: it still carries a session factory, repository path,
settings, and other resource locators. Those capabilities must leave the executor
side before the fence can be removed.

## Canonical history in PostgreSQL

### Recommendation

Replace the canonical git repository with an append-only `DocumentVersion` model.
Use complete snapshots first; optimize with deltas only if measured storage makes
that necessary.

```text
DocumentVersion
  id
  incarnation_id
  parent_version_id
  proposal_markdown
  specification_markdown
  citations_json
  auxiliary_artifacts_json
  content_hash
  created_at
  actor_kind
  actor_id / origin_job_id
  message
```

`WorkspaceIncarnation.head_version_id` selects the current version. Restoring an
old version creates a new version whose parent records where it came from.
Browsing history and producing diffs are ordinary indexed reads plus a text diff.

The current product code uses linear `HEAD`, immutable blobs, commit history, and
diffs. It does not depend on user-facing branches or merges. PostgreSQL snapshots
therefore preserve the actual R1 behavior without reproducing all of git.

### What this removes

- PostgreSQL/git dual-write recovery;
- working-tree and index rollback;
- git subprocess timeouts while holding the workspace mutex;
- repository path authority in application and LLM code;
- filesystem coordination during restore and purge;
- orphan commits whose corresponding database transaction failed.

A document version, its metadata, Yjs checkpoint watermark, and related domain
rows can commit in one PostgreSQL transaction.

The Yjs log is already in PostgreSQL in
[collab/store.py](../../../src/call_manager/proposal_assist/collab/store.py).
Moving snapshots there gives the collaborative log and its durable versions one
atomicity domain.

This is related to Event Sourcing, but full event sourcing is unnecessary. Fowler
notes that an event log may be the system of record or may supplement current
state, and that version-control systems are a common temporal example:
<https://martinfowler.com/eaaDev/EventSourcing.html>.
For this application, append-only document snapshots plus ordinary relational
state are simpler than replaying every domain event.

### What this does not remove by itself

A stale actor can insert a PostgreSQL version after delete and restore just as it
can commit to git. One transactional store fixes atomicity; immutable incarnation
references and exclusive mutation authority fix lifecycle ABA.

### Keeping git is possible

The state-service/incarnation architecture can eliminate the lifecycle fence while
retaining git if:

- only the state service can access repository paths;
- every git mutation is a short mailbox command;
- sealing waits only for the command currently executing, never for an LLM;
- old actors can submit only to their closed incarnation mailbox.

That is a valid lower-migration intermediate state. It retains the dual-write and
filesystem complexity, so it is not the lowest-complexity destination.

Git receive quarantine is useful prior art for staging objects before promotion,
but the authoritative ref update still needs a single promoter:
<https://git-scm.com/docs/git-receive-pack#_quarantine_environment>.

## Lifecycle behavior

### Owner delete

One `Seal` mailbox command:

1. Stop admitting commands and sessions to incarnation A.
2. Commit any current in-memory CRDT state as A's final document version.
3. Mark A sealed and store its final head.
4. Set `Workspace.current_incarnation_id = NULL`.
5. Close A's sessions and mailbox.
6. Commit and return.

Nothing is destroyed. Cancellation of LLM work happens afterwards to save tokens,
not to establish correctness.

### Admin inspection

Read the sealed incarnation and its immutable version history. No special frozen
filesystem path is required with PostgreSQL history.

### Admin restore

One state-service transaction:

1. Read sealed incarnation A and its final head.
2. Create incarnation B with that version as its base and initial head.
3. Recreate active collaboration/session state as empty.
4. Set `Workspace.current_incarnation_id = B.id`.
5. Commit, then instantiate B's mailbox.

Old jobs and sessions still refer to A. No data is copied merely to achieve
isolation; immutable versions may be shared safely.

“Exactly as it was” means the restored incarnation begins from A's committed final
version and retained metadata. New activity starts a new lifetime, which should be
visible in audit history.

### Admin purge

Purge permanently removes or tombstones all incarnations and their retained bytes
through the state service. Because no executor has storage or domain-write
authority, a late candidate cannot recreate purged state.

Purge should be idempotent and durable, but it does not require proving that every
LLM call has stopped. Late results are dead letters.

## Requirements check

| Requirement | Result |
|---|---|
| R1: version history | Preserved by append-only versions; git may remain during migration. |
| R2: three writer kinds | All submit short commands; long computation remains concurrent outside mailboxes. |
| R3: hidden, inspectable, exact restore | Sealed incarnation retains all data; restore starts a new incarnation from its final version. |
| R4: no pre-delete effect after delete or restore | Old sessions/jobs address only the permanently closed incarnation. |
| R5: small single-machine operation | One local Python state service and PostgreSQL; no cluster or workflow platform. |
| R6: git is optional | PostgreSQL versions remove cross-store atomicity; git can be retained temporarily. |

## Alternatives and whether they eliminate the fence

### Restore to a new public workspace ID

**Potentially eliminates the ABA identity reuse, but not by itself.** Old writers
can still mutate the hidden old workspace unless that aggregate is physically
immutable or inaccessible.

It becomes sound if delete transactionally moves the old aggregate into an
immutable archive that ordinary writers cannot address, and restore creates a new
active aggregate. With canonical history and state in PostgreSQL, this is a real
alternative.

Costs:

- URLs and external references change;
- ownership, collaborators, attachments, proposal links, and audit relationships
  need cloning or aliasing;
- “restore” becomes “copy from archive,” which may surprise users.

Keeping a stable public ID with internal incarnations obtains the same safety
without changing product identity.

Google Drive documents trash and untrash as updates to the same file resource and
`fileId`, rather than creating a visible replacement ID:
<https://developers.google.com/workspace/drive/api/guides/delete>.
That does not reveal Google's internal mechanism, but it shows that preserving the
public identity is normal product behavior.

### PostgreSQL history alone

**Removes:** git/DB atomicity, repository locks, filesystem recovery.  
**Does not remove:** stale authorization across delete and restore.

Recommended, but only as one half of the target.

### OS rename, unlink, or per-incarnation paths

**Relocates the problem.** Already-open descriptors and subprocesses survive a
rename. Database effects are unaffected. Reusing a path after restore creates the
same ABA issue at the pathname level.

Per-incarnation paths become sound only when old actors cannot resolve the new
path, which is again capability separation.

### Single queue containing the LLM call

**Eliminates interleaving but breaks availability.** A minutes-long model call
would sit ahead of delete. Process failure also loses the simple ordering unless
the queue and state are durable.

The recommended design serializes only short acceptance/effect commands and lets
LLM computation run concurrently.

### Stop and repair on restore

**Does not satisfy R4.** A stale write takes effect while hidden, and repair must
know every store and semantic row to undo. It also cannot safely distinguish stale
post-delete work from legitimate audit/accounting without retaining provenance.

Repair remains useful for operational recovery, not correctness.

### Go or Rust LLM manager

**Does not remove the fence.** A manager with database and repository access can
still write from stale authority. A manager without that access is the candidate
executor in the recommended design, for which Python is adequate.

The useful service is a workspace state service, not an LLM manager. Its value is
exclusive authority and incarnation ownership, not memory safety or throughput.

### Process per workspace or container per workspace

**Can eliminate the fence**, because process lifetime can equal incarnation
lifetime and OS permissions can isolate storage. It is unnecessary operational
machinery here. One process can host many incarnation mailboxes while retaining
one DB-role boundary.

### Same-process object capabilities

**Reduces risk but cannot make bypass impossible in Python.** A module can import a
session factory, global registry, or repository helper unless structural tests
forbid it. That returns to the scanner problem.

A separate process, restricted PostgreSQL role, and filesystem permissions make
the boundary real. Python is not the obstacle; ambient authority in one process is.

## Migration strategy

Keep the current fence fully operational until the last direct writer is removed.
The migration should reduce old surface monotonically rather than create a second
permanent correctness system.

### Slice 1: prove the state-service boundary

Build a minimal local Python service with:

- one incarnation table and one active incarnation;
- one mailbox;
- one job-bound candidate command;
- a restricted database role;
- no LLM or git integration.

Run the falsifying experiment from the judgment gate. Use a paused fake worker,
not a live model call.

Do not proceed if the worker can resolve the current incarnation, if it can write
domain tables directly, or if delete waits for it.

### Slice 2: move canonical document versions

Add PostgreSQL `DocumentVersion` snapshots and shadow existing git commits while
the fence remains authoritative. Verify histories and rendered heads against real
repositories before switching reads.

After cutover, keep git repositories read-only until migration verification and
rollback windows close. Do not dual-write indefinitely.

### Slice 3: move one complete writer path

Move an end-to-end document path, including authorization, version insertion,
metadata, and response, behind the state service. A good path exercises both
content and metadata rather than only a DB status update.

The web process must be unable to perform that path directly under its restricted
role. A test that merely chooses not to bypass the service does not prove the
boundary.

### Slice 4: move WebSocket and LLM admissions

- Admit each WebSocket connection to one incarnation and route updates by session
  ID.
- Create each background job through the mailbox and derive its incarnation from
  the job record.
- Remove sessions, repository paths, and storage handles from executor contexts.
- Persist LLM results as candidates or send them over IPC; apply them only through
  their originating job mailbox.

### Slice 5: switch delete and restore

Make owner delete seal the current incarnation and admin restore create a new one.
Run delete/restore/resume tests across HTTP, WebSocket, setup, turns, analysis,
and all pass families.

### Slice 6: remove the fence

Only after database permissions and filesystem permissions prove there are no
remaining direct workspace writers:

- delete `FenceStamp` from effect APIs;
- remove generation columns and primitives;
- delete provenance and sink scanners that exist solely for the fence;
- retain ordinary architectural tests for the process/DB-role boundary;
- keep cancellation and retirement only as cost and resource cleanup.

The final boundary test should run a forbidden direct write using the web or LLM
role and observe PostgreSQL reject it. Unlike an AST scanner, this test remains
valid when Python syntax and call graphs change.

## Cheapest decisive spike

Use a fake long-running worker and one real PostgreSQL-backed document command:

1. Create workspace W and active incarnation A.
2. Have the state service admit job J to A and persist an immutable input snapshot.
3. Pause the worker after reading J.
4. Submit ordinary HTTP and collaborative commands to A; verify mailbox ordering.
5. Seal A and return from delete while the worker remains paused.
6. Restore W as incarnation B from A's final version.
7. Resume J and submit its result using only J's ID.
8. Assert the result is recorded as discarded/dead-lettered against A.
9. Assert B's document, metadata, Yjs state, and history are unchanged.
10. Attempt direct domain writes with the worker and web credentials; assert the
    database rejects them.
11. Introduce a deliberately unsafe stable-workspace resolver to the worker test
    fixture and verify the architecture test fails.

This spike kills the proposal cheaply if the authority boundary is porous. It
also tests the exact property the current scanners protect, without first
building the full migration.

## Final recommendation

Adopt the following destination if reducing lifecycle complexity justifies a
substantial write-path migration:

1. **PostgreSQL append-only document versions** as canonical history.
2. **Immutable workspace incarnations** behind one stable public workspace ID.
3. **One local Python workspace state service** with per-incarnation mailboxes.
4. **Job- and session-derived routing**, never caller-supplied incarnation identity.
5. **Candidate-only LLM workers** with no domain-write or filesystem authority.
6. **Database and OS permissions** as the enforcement mechanism.

This removes the lifecycle fence rather than improving it. The price is moving all
workspace-scoped writes through one service. The payoff is that restore no longer
revalidates old authority, git and database no longer form a split transaction,
and correctness no longer depends on proving value provenance across arbitrary
Python call graphs.

If that migration is too large, keep the current fence. A partial incarnation
refactor without exclusive mutation authority is worse: it adds another identity
layer while retaining the same fence under a different name.
