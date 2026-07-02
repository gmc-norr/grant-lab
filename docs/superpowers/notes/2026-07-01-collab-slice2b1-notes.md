# Slice 2b-1: Room-Aware Write Kernel Notes

**Date:** 2026-07-01  
**Branch:** collab-slice2b1  
**Feature:** Server-side atomic proposal accept through the real-time Yjs room, not via git.

---

## What Shipped

### The Room-Aware Write Kernel Prose-Accept Path

The kernel accepts a `prose_edit` proposal against the **live Yjs room**, validating staleness and applying the edit atomically within a single transaction. Five core primitives enable this:

1. **`apply_pm_json_to_fragment(doc, pm_json) -> bytes`** (`collab/yjs_bridge.py`)  
   Clears and repopulates the live `"prosemirror"` XmlFragment in one Yjs transaction, returning the **incremental update** captured by an observer. A client synced to the pre-edit state converges by applying this single update.

2. **`store.append_update(..., commit=False)`** (collab state store)  
   Stages an update on the caller's transaction via flush, allowing the accept to commit atomically with the update and checkpoint in one `session.commit()`.

3. **`kernel_accept_prose(...)`** (`collab/kernel.py`)  
   Accepts a `prose_edit` proposal against the live Yjs room, not git. Validates staleness against the LIVE section SHA (via `split_sections(pm_to_markdown(ydoc_to_pm_json(room_doc)))`), applies the edit, stages the update, checkpoints the artifact — all on the caller's single transaction (one `session.commit()`; the kernel only flushes). Returns `KernelAcceptResult(pc, update, committed, evict)`. Atomic accept = **FOR UPDATE row lock + staged Yjs update + `checkpoint_artifact` + `accepted`/`resulting_commit_sha` status flip**.

4. **`RoomManager.operate(key, *, load)` async context manager** (`collab/room.py`)  
   Server-op room lifecycle: get-or-create a room WITHOUT registering a client, bumping a `Room.ops` refcount. On exit it evicts the room only when `not clients and ops == 0` (see H1 below). It yields the `Room`: `async with MANAGER.operate(key, load=_load) as room: ...`.

5. **`accept_via_kernel(...)`** (`proposals/kernel_accept.py`) + async route in `web/proposals.py`  
   Routes accept through the kernel when `is_collab_enabled`, else the git-first path unchanged. Orchestrates the single transaction and broadcast of the incremental update to connected clients.

### Concurrency Hardening (Codex Plan Review, 2026-07-01)

Three critical race conditions were identified and fixed:

**H1 — `Room.ops` Refcount (Eviction Deferred Until No Concurrent Ops)**  
`operate`-exit or a last-client `release()` could evict the room while another server op (e.g., a concurrent accept) still holds it. A new client connecting to an evicted room re-hydrates fresh from the log, missing any not-yet-committed update from the still-in-flight op.

*Fix:* `Room.ops` increments when an op acquires the room lock (same place a client increments `clients`). Both `operate`-exit (via `finally`) and `release()` defer eviction until `not clients and ops == 0`. Deterministic asyncio unit test in Task 5 Step 2 verifies overlapping ops + release-during-op.

**H2 — Room-Doc Read-Locking (SYNC_STEP1 + Connect Send)**  
The kernel mutates `room.doc` on a worker thread under the asyncio lock; however, the SYNC_STEP1 read (`Room.handle`) and `Room.initial_message()` at WS connect read the doc **unlocked on the event-loop thread**. Once threadpool mutation exists, this is a genuine cross-thread pycrdt race.

*Fix:* Guard both doc reads with the room lock. `accept_via_kernel` holds `room.lock` across the single `run_in_threadpool(_body)` call that mutates the doc from the worker thread, so all event-loop-thread reads are serialized against the mutation. The room `asyncio.Lock` alone is NOT sufficient — it only serializes coroutines on the event loop, while the mutation runs on a worker thread and the GIL does not make a multi-step pycrdt mutation atomic against a concurrent read.

**H3 — Commit Mutex Acquired Before Spec-Lock Guard**  
`post_lock_spec` sets the lock **under** the commit mutex. The kernel checked `spec_locked_at` before acquiring the mutex in `checkpoint_artifact`, so a `POST /spec/lock` committed between the guard and checkpoint could be missed.

*Fix:* The kernel acquires the commit mutex **immediately after** the staleness early-return, before reading `spec_locked_at`. Later, `checkpoint_artifact` re-acquires the same advisory **xact** lock (re-entrant within the transaction: instant no-op, no deadlock/second timeout). `CommitMutexTimeout` added to `_CLEAN_ACCEPT_ERRORS` so it does not trigger room eviction.

### Validation & Invariants

The accept validates against the **live room**, not git HEAD, and proves three key invariants:

1. **Stale-vs-Live Regression Guard**  
   A live-but-uncheckpointed edit leaves `git HEAD == base_commit_sha` yet is detected stale via the live section SHA. Test case: edit the room, accept against it, verify git unchanged; then attempt accept from the pre-edit SHA and observe stale rejection.

2. **No-Op Accepts (Idempotent at Checkpoint)**  
   Proposing exactly the live section text accepts at the current checkpoint with **no new commit**: the result has `committed=False`, `update=None`, and **no new `ArtifactVersion` row is created** (`resulting_commit_sha` is the current HEAD).

3. **Accept Race via FOR UPDATE + Status Gate**  
   Two accepts of the **same pending proposal**: the `_load_scoped` FOR UPDATE row lock plus the `pc.status != "pending"` gate make the **second accept a clean 409** (`ProposedChangeNotPending`) — no double-apply, exactly **one** `ArtifactVersion`, and the proposal's status stays `accepted`. (Validated as the serialized invariant: first accept succeeds, second is rejected cleanly.)

4. **Client Convergence via Incremental Update**  
   The captured update is incremental (not a full doc snapshot). A client with the pre-accept doc state applies it and converges to post-accept state.

5. **Ops-Refcount Deferred Eviction**  
   Overlapping server ops + simultaneous client release: room not evicted until all ops exit and all clients disconnect. Verified by deterministic asyncio test.

---

## Flag Status: OFF in Production

Collab is a **per-workspace, per-artifact DB flag** (the `collab_artifact_state` row's `enabled_at`), read via `state.is_collab_enabled(...)` and set via `state.enable_collab(...)` — there is no env-var toggle. In 2b-1 the flag is **never enabled in app/prod code**; the kernel accept path is exercised **only** by tests that call `state.enable_collab(...)` explicitly. The prod flag-flip and final validation against live co-editing clients is **Slice 3** (needs the Tiptap editor).

---

## Deferrals

**Slice 2b-2 (spec-lock operation + citation-insert accept)**  
- `kernel_accept_prose` assumes citation_insert and spec-lock operation accepts are out of scope; they are deferred to 2b-2.
- A collab-enabled citation_insert accept will raise `CitationKernelDeferred` (409) instead of falling through to git-first (to avoid half-applying a citation insert via two paths).

**Slice 3 (git-canonical switchover + flag flip + real co-editing)**  
- `save_section` (human edit path) still writes to git, not the room. Switchover to room-canonical writes deferred to Slice 3.
- Flag flip (when real co-editing clients connect) deferred to Slice 3.

**Deferred (Tier-1/Tier-2 post-checkpoint validation)**  
- The kernel checkpoints via `checkpoint_artifact`, which is **provenance-only** (writes an `ArtifactVersion` row — `created_by="user"` — plus a `WorkspaceEvent`, but does not re-run evaluation).
- Tier-1 and Tier-2 re-fire on new checkpoints still deferred; kernel accepts commit the artifact without re-evaluation.

**Out of Scope**  
- Agent direct-commit paths (coherence/evaluator jobs) are unaffected and out of scope; they commit to git and create checkpoints directly, bypassing the kernel.

---

## Known Limitations (Slice 3 Follow-Ups)

### 1. Whole-Fragment Replace → Invisible in 2b-1, Must Fix in Slice 3

The kernel applies an accept by **clearing and repopulating the entire `prosemirror` XmlFragment**. The captured incremental update encodes the deletion of every existing CRDT node and creation of new ones.

**Invisible in 2b-1:** flag off → no concurrent editing clients; room lock serializes all server ops.

**Problem:** In Slice 3 (flag on, live co-editing), a client edit made against the pre-accept doc that arrives **after** the accept releases the room lock references now-deleted nodes. On merge, those nodes are effectively **lost** (the merge is between a deleted-node update and a new-node update; the deleted nodes are orphaned in CRDT state).

**Required fix:** Replace whole-fragment clears with a **targeted section-subtree replace** (mutate only the changed section nodes) or implement accept-rebase (transform in-flight client edits to the post-accept state). Record as **top Slice-3 kernel follow-up**.

### 2. Evict Does Not Disconnect Clients — Codex Open Question

`MANAGER.evict(key)` drops the room from the registry; already-connected WS tasks keep their reference to the now-orphaned room object. This is accepted Slice-1 poison debt (the room object leaks; it never closes the HTTP connection or signals the client to reconnect).

**Unreachable in 2b-1:** the kernel's evict-on-failure IS implemented (`accept_via_kernel`'s `except Exception: await MANAGER.evict(key)`), but it is unreachable in practice — flag off ⇒ no clients are connected during a kernel accept, so an orphaned room has no clients to strand.

**Problem in Slice 3:** Flag on, live co-editing, kernel op failure → evict is called; connected clients still hold a reference to the evicted room and continue buffering updates to it instead of re-syncing. The broadcast channel is never notified; clients go stale.

**Required fix:** Make `evict()` actively close the broadcast channel and signal connected WS tasks to disconnect and re-connect. Record as **Slice-3 prerequisite alongside whole-fragment-replace**.

### 3. Staleness compares NORMALIZED live SHA vs RAW-git base SHA → spurious "stale" (TOP Slice-3 blocker)

`kernel_accept_prose`'s staleness guard compares `live.sha` — computed from the room's **normalized** serialization (`pm_to_markdown(ydoc_to_pm_json(room_doc))`, which inserts a blank line after each block) — against `pc.base_section_sha`, which every real propose path derives from **raw git-HEAD** markdown (`split_sections(read_text())` in `tools/proposal.py` / `proposing.capture_base` / the raw section reads in `web/api/workspaces.py`). Because the room hydrates from raw HEAD but re-serializes with the blank-line normalization, `live.sha != base_section_sha` **even when nobody edited the section**, so the accept is marked terminally `stale`. Worse, the only thing that would normalize HEAD is a successful checkpoint, and the first prose accept — the very thing that would checkpoint — is the one rejected, so it **never self-heals**. Reproduced end-to-end (raw sha vs normalized live sha differ).

**Invisible in 2b-1:** flag off ⇒ the kernel path never runs in prod; git-first accept (which compares `base_commit_sha` to git HEAD) is used and is unaffected.

**Masked by the tests:** the kernel/route tests deliberately compute `base_section_sha` from the live room round-trip (see `_live_sections_from_md`), which no real propose path does — so the suite is green while the real-world path would fail.

**Required fix (Slice 3, before the flag flip):** make the base and live SHAs share one basis. Either make the propose path **room-aware** (capture `base_section_sha` from the live room — the natural outcome once proposals originate in the Tiptap WYSIWYG editor), or **normalize git at hydrate time** (seed a `pm_to_markdown(markdown_to_pm(HEAD))` checkpoint so raw-git and room serializations agree). Add a kernel test whose `base_section_sha` comes from RAW git HEAD (via `capture_base` on the git-first file) with collab enabled, asserting the accept **succeeds**. **This is the #1 correctness prerequisite for turning the flag on.**

---

## Post-merge code review (2026-07-02) — adversarial multi-agent pass

A 7-angle adversarial review (find → refute/reachability verify → synthesize) ran over the branch diff. **No production-reachable defect** (flag off; git-first path byte-for-byte intact); H1/H2/H3 confirmed correctly implemented. Findings resolved on-branch before merge:

- **Fixed — `RoomManager.operate` cold-race (Important):** the slow-path `else` branch bumped `room.ops` in a *separate* lock section from the re-fetch, so a concurrent `operate`-exit could evict the room in the gap (defeating the H1 invariant). Now the re-fetch + `ops` bump share one lock section; the identical pattern in `acquire` (client-add) was hardened too, since the ops-eviction newly widens that window. New test: `test_operate_else_branch_bumps_ops_atomically`.
- **Fixed — broadcast held `room.lock` (Minor liveness):** `accept_via_kernel` broadcast the accept update inside `async with room.lock`, so one backpressured client could stall every reader/writer on the room. Moved the broadcast outside the lock (mirrors `Room.handle`'s write path); the `operate` context still keeps the room alive across it.
- **Fixed — redundant post-commit `s.refresh` (Minor):** removed (`expire_on_commit=False` + columns-only `_serialize` make it a wasted SELECT; the git-first path proves it unnecessary).
- **Fixed — plan lock-order doc (Minor):** the plan's Global-Constraint line stated `FOR UPDATE → room.lock → mutex`; the real order is `room.lock → FOR UPDATE → mutex`. Corrected, with the deadlock-freedom argument restated (git-first writers never take `room.lock`).
- **Coverage added (Minor gaps):** spec-lock kernel path — reject into a locked spec + succeed on an unlocked spec (exercises the H3 mutex-before-guard order); commit-mutex-timeout → 429; and evict-on-failure (post-mutation `GitCommitError` → 500 + room evicted + transaction rolled back).
- **Deferred (documented above):** the staleness normalization issue (#3) — genuinely requires a room-aware propose path or hydrate-time normalization = Slice-3 scope, and is the top prerequisite for the flag flip.
- **Refuted / dropped:** empty-update crash and partial-mutation-on-malformed-pm (both unreachable — the sole caller passes schema-validated `markdown_to_pm` output); double `require_member` on the git-first path (harmless, arguably safer for authz freshness); the sequential accept-race test (plan-sanctioned; the row lock is independently covered); the `.gitignore` trailing-slash change (load-bearing for the worktree `node_modules` symlink the project convention relies on).

---

## References

- **Spec:** `docs/superpowers/specs/2026-06-30-collaborative-editing-foundation-design.md`
- **Plan:** `docs/superpowers/plans/2026-07-01-collab-slice2b1-write-kernel-accept.md`
- **Prior-slice notes:** `docs/superpowers/notes/2026-06-30-collab-slice1-sync-server-notes.md`, `docs/superpowers/notes/2026-07-01-collab-slice2a-notes.md`
- **Code:**
  - `src/call_manager/proposal_assist/collab/yjs_bridge.py` — `apply_pm_json_to_fragment`
  - `src/call_manager/proposal_assist/collab/store.py` — `append_update(commit=False)`
  - `src/call_manager/proposal_assist/collab/kernel.py` — `kernel_accept_prose`
  - `src/call_manager/proposal_assist/collab/room.py` — `RoomManager.operate`, `Room.ops`/`Room.lock`/`Room.broadcast`
  - `src/call_manager/proposal_assist/proposals/kernel_accept.py` — `accept_via_kernel`
  - `src/call_manager/proposal_assist/web/proposals.py` — async accept route
  - `src/call_manager/proposal_assist/web/collab_ws.py` — connect-site read-lock (H2)
- **Tests:**
  - `tests/proposal_assist/collab/test_collab_apply_fragment.py` — fragment replace + incremental convergence
  - `tests/proposal_assist/collab/test_collab_store_stage.py` — `commit=False` staging
  - `tests/proposal_assist/collab/test_collab_kernel_accept.py` — kernel core (stale-vs-live, no-op)
  - `tests/proposal_assist/collab/test_collab_kernel_accept_route.py` — route + convergence + race + flag-off parity
  - `tests/proposal_assist/collab/test_collab_room_operate.py` — ops-refcount deferred eviction
