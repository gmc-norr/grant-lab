# Collab Slice 3a — Backend Hardening for the Flag-Flip (Design)

**Date:** 2026-07-02
**Status:** Design approved; ready for implementation plan
**Slice:** 3a of the collaborative-editing foundation (see `[[project_collab_editing_foundation]]`)

## Goal

Close every known correctness gap that only manifests with the collab write
flag **ON**, entirely server-side, so Slice 3b can flip the flag safely. This
slice ships **no frontend** and **flips no flag** — its deliverable is a backend
that is provably safe to enable.

## Architecture

The collaborative write path (Slices 2a–2c) is complete but the per-artifact
flag (`collab_artifact_state.enabled_at`) stays OFF in prod because seven
defects only surface once real clients connect and edit concurrently: two cause
data loss or false rejection, three cause permission leaks / lost checkpoints,
and two are robustness/UX. Slice 3a fixes all seven with logic-only changes to
the existing `collab/` modules and the proposal read path. No schema, no
migration, no new dependency, no frontend.

## Global Constraints

- **The collab flag STAYS OFF in prod through Slice 3a.** Every change here is
  reachable only when `is_collab_enabled(...)` is true, which prod never sets.
- **Flag-off parity is a hard gate.** `tests/proposal_assist/proposals/test_accepting.py`,
  `tests/test_api_spec_lock.py`, and the member/rubric/soft-delete suites MUST
  stay green **unchanged**. The flag-off proposal-read and accept paths must be
  byte-for-byte identical after this slice.
- **No migration.** All changes are in-memory / logic; no DB schema touch.
- **No frontend.** All new behavior is exercised by server-side tests using the
  socket-free `Room`/`RoomManager` and kernel harnesses.
- Tests use `EMBEDDING_MODEL=bge-m3`; verify via grouped-dir runs, never the
  whole suite (testcontainers PG flake under load).
- CI runs `ruff check .` (not format).

## Background: the seven defects

Carried over from the Slice 2b-1 / 2b-2 / 2c post-merge reviews (documented in
their notes and in memory):

| # | Defect | Class | If unfixed when flag flips |
|---|--------|-------|-----------------------------|
| 1 | Staleness-normalization: base SHA from raw git HEAD vs `live.sha` from normalized round-trip | Blocker | Silently rejects legitimate proposals as "stale"; never self-heals |
| 2 | Whole-fragment replace clobbers concurrent edits | Blocker | A kernel accept deletes a client's concurrent typing (data loss) |
| 3 | (a) Unserialized reconciliation | Blocker | Rapid unlock→lock leaves a stale `can_write` (permission leak) |
| 4 | (b) Connect-straddle TOCTOU | Blocker | Client mid-`acquire` misses a reconciliation signal (permission leak) |
| 5 | (d) Resolve fail-open | Blocker (security) | A DB blip during reconcile keeps stale (elevated) write access |
| 6 | (c) Ops-straddle checkpoint skip | Robustness | A last-leave checkpoint is silently lost when a kernel op straddles the last departure |
| 7 | (e) Kernel-accept evict blast-radius | UX | One accept failure disconnects all clients (they reconnect clean) |

## Component 1 — Room-aware proposal reads (fixes #1)

### Root cause

`base_section_sha` on a proposal is captured **HEAD-first**: `_read_artifact_impl`
(`tools/proposal.py`) does `content = path.read_text()` on the raw git file, then
`setattr(ctx, sections_attr, {s.index: s.sha for s in split_sections(content)})`.
The kernel's accept-time staleness check compares that against
`live.sha`, where `_live_sections(room_doc)` =
`split_sections(pm_to_markdown(ydoc_to_pm_json(room_doc)))` — the **normalized**
round-tripped form (`pm_to_markdown` inserts a blank line after each block,
loosens lists, etc.). Whenever the committed markdown is not already in that
canonical form, `base_section_sha != live.sha` for unchanged content →
`kernel.py:71` (prose) and `kernel.py:183` (citation) reject a legitimate
proposal as stale, and the first accept that *would* normalize HEAD is itself
the one rejected → never self-heals.

### Fix: read the room-canonical form when collab is enabled

When `is_collab_enabled(session, workspace_id, artifact)` is true, the agent's
read snapshot must come from the **room-canonical** representation, so its
section SHAs are normalized and match `live.sha`.

**Mechanism — log-derived, synchronous, side-effect-free.** The durable Yjs
update log (`yjs_updates`) IS the room's canonical committed state: every
accepted edit (client `handle` or kernel `checkpoint`) persists before it is
broadcast. So the read does not touch the live in-memory room or the event loop
at all:

1. If `is_collab_enabled` and the log is non-empty:
   build a transient `pycrdt.Doc` from `load_rows(session, workspace_id, artifact)`,
   apply the updates, then
   `content = pm_to_markdown(ydoc_to_pm_json(doc))`.
2. If `is_collab_enabled` and the log is empty (flag just enabled, no edits yet):
   read git HEAD and normalize it through the same round-trip
   (`pm_to_markdown(markdown_to_pm(raw_head))`) so it matches what a future room
   would serialize.
3. If `not is_collab_enabled`: **unchanged** — `content = path.read_text()`
   (raw HEAD), exactly as today.

The single choke point is `_read_artifact_impl` in `tools/proposal.py` (both
`read_proposal_impl` and `read_spec_impl` delegate to it). Only the source of
`content` changes when collab is on; the downstream `sections_attr` /
`proposal_attr` population flows from that `content` unchanged, so the
`(section_index, base_section_sha)` internal-consistency invariant is preserved.
`head_attr` continues to be `current_head(ctx.repo_dir)` (the checkpoint HEAD);
the collab accept path keys staleness on `base_section_sha` vs `live.sha`, not
on `base_commit_sha`, so HEAD tracking is unaffected.

The DB read (`is_collab_enabled` + `load_rows`) uses a synchronous session from
`ctx.session_factory`, consistent with the existing synchronous `path.read_text()`
in this async function.

### Parity

Flag-off reads keep the raw-HEAD path byte-for-byte. The
`_read_artifact_impl` HEAD-before-content ordering invariant (fail-safe against a
concurrent user save) is retained on the flag-off path and is moot on the
collab path (staleness is section-SHA based).

## Component 2 — Section-scoped fragment replace (fixes #2)

### Root cause

`apply_pm_json_to_fragment` (`yjs_bridge.py:225`) applies a **whole-doc**
replacement: `while len(fragment.children) > 0: del fragment.children[0]` then
re-inserts every block from a pre-edit snapshot. As a Yjs update this deletes
every existing block and inserts fresh ones — any concurrent client edit racing
the transaction is deleted and not re-inserted, so edits to *other* sections are
lost.

### Fix: replace only the target section's block range

New `apply_pm_section_to_fragment(doc, *, section_index, blocks)` in
`yjs_bridge.py`:

1. Walk the fragment's top-level children and, using the **same `##`-heading
   boundary rule as `split_sections`**, compute the `[start, end)` top-level
   block range that belongs to `section_index`.
2. In one transaction, delete only that range and insert `blocks` (the new
   section's PM blocks).
3. Capture the incremental update via the existing observer (unchanged pattern).

Both kernel accepts switch from the whole-doc apply to the section-scoped apply:
`kernel_accept_prose` and `kernel_accept_citation` reassemble only the changed
section's blocks and call the new function with the proposal's `section_index`.

### Why this is provably sufficient

The kernel has already verified `live.sha == base_section_sha` for the **target**
section before mutating (Component 1 makes this comparison meaningful). So the
target section is provably unchanged since the proposer read it — there are no
concurrent edits *there* to preserve (or the accept would have been rejected as
stale). Replacing only the target section therefore clobbers nothing on the
target, and leaving all other sections untouched preserves their concurrent
edits — which is exactly the data-loss bug. Finer (block- or character-level)
diffing would only add precision within a section that cannot hold a concurrent
edit, so it is unnecessary (YAGNI).

### Section boundary edge cases (to nail in the plan)

- Preamble (`section_index == 0`, content before the first `##`).
- A new section body with a different block count than the old range.
- The target being the last section (end = fragment length).
- An empty section body.

## Component 3 — Reconciliation-robustness cluster (fixes #3–#7)

### (a) Serialize reconciliation — fixes #3

`refresh_permissions` (`collab/permissions.py`) snapshots
`list(room.clients.items())` then `await`s `run_in_threadpool(_resolve)` per
client, so two signals (e.g. unlock then lock) interleave at the await points
and can leave a conflicting `can_write`.

**Fix:** a per-room reconciliation lock (a dedicated `asyncio.Lock`, e.g.
`Room._recon_lock`, distinct from `Room._lock` so DB-bound resolves don't block
doc writes). `refresh_permissions` holds it for the whole pass. Each `_resolve`
still reads fresh DB state, so serialized passes converge on the final committed
state (the last pass wins, correctly).

### (b) Connect-straddle TOCTOU — fixes #4

`RoomManager.acquire` (`room.py:185`) registers a client with its handshake-time
`state`; a mutation committing during `acquire` can fire its `refresh` *before*
the client is registered in `room.clients` (line 197 / 217), so the client is
missed and keeps stale access.

**Fix:** extract a `reconcile_client(session_factory, workspaces_dir, room, client, state, *, workspace_id, artifact)`
helper (one client's re-resolve + apply, the body of the per-client loop in
`refresh_permissions`, reused by both). After `acquire` registers the client,
the WS route calls `reconcile_client` for it under the per-room recon lock.
Because registration happens under `manager._lock` and reconciliation holds the
recon lock, ordering vs a concurrent `refresh` is well-defined: either `refresh`
sees the newly registered client, or the connect's own re-resolve sees the
already-committed mutation. Neither can miss it.

### (c) Ops-straddle checkpoint skip — fixes #6

`release(on_evict=checkpoint)` (`room.py:261`) only evicts when `ops == 0`; if a
kernel accept holds `ops > 0`, the room is not evicted and `on_evict` never
fires — and `operate()`'s `finally` (line 253-256) that later pops the room does
**not** carry the pending checkpoint callback, so the last-leave checkpoint is
silently lost.

**Fix:** when `release` cannot evict because `ops > 0` but the room is now
client-empty, stash the callback on `room._pending_on_evict`. `operate()`-exit,
when it finally pops the room (`not room.clients and room.ops == 0`), invokes the
stashed callback (outside the manager lock, like `release` does). Guard against
double-invocation and clear the field on eviction.

### (d) Resolve fail-closed — fixes #5

`refresh_permissions` (`permissions.py:106-112`) currently catches a per-client
resolve error and `continue`s, leaving the client **as-is** (fail-open): a DB
blip during a downgrade/revoke keeps stale (possibly elevated) write access.

**Fix:** on resolve error, set `state.can_write = False` (downgrade to
read-only) instead of leaving as-is. No write can leak; the connection is kept
(a transient blip does not disconnect an innocent editor); the next successful
signal restores write. A false downgrade errs toward *less* access and is
recoverable by the next signal or a reconnect.

### (e) Kernel-accept evict blast-radius — fixes #7

`proposals/kernel_accept.py` runs `except Exception: await MANAGER.evict(key)`
on the post-mutation arm, disconnecting **all** clients on one accept failure
("room.doc may be ahead of the rolled-back log").

**Fix:** keep `evict` as the safe post-mutation recovery (clients reconnect and
rebuild clean from the durable log). Confirm — with a guard/comment and a test —
that it fires **only** on genuine post-mutation corruption: pre-mutation
failures are already in `_CLEAN_ACCEPT_ERRORS` and take the non-evicting arm.
Document the blast radius. Re-hydrate-in-place is explicitly out of scope for
3a (a rare path; the reconnect-from-log recovery is already correct).

## Testing

All server-side; no frontend, no live sockets required (the `Room`/`RoomManager`
+ kernel harnesses and `FakeClient` cover it).

- **Component 1:** with collab enabled and a durable log whose normalized form
  diverges from raw git HEAD, a proposal read yields a base that the kernel
  **accepts** (not false-stale) — the exact #1 regression. Plus: flag-off read
  still returns raw HEAD (parity); empty-log-enabled read returns
  normalized-HEAD.
- **Component 2:** a two-client room where client B has a concurrent edit in
  section 2 while a kernel accept lands on section 1 — assert B's section-2 edit
  survives. Plus boundary cases (preamble, last section, block-count change,
  empty body).
- **Component 3:**
  - (a) two rapid signals (unlock then lock) → final `can_write` matches the
    last committed state, deterministically.
  - (b) a client connecting while a revoke commits → the connected client ends
    read-only/closed (no stale write).
  - (c) revoke of the last client while a kernel op holds `ops > 0` → the
    checkpoint still fires when the op drains.
  - (d) a resolve raising during reconcile → the client is downgraded to
    read-only, not left writable, not disconnected.
  - (e) a pre-mutation accept failure does **not** evict; a post-mutation
    failure does.
- **Parity gate:** `test_accepting.py`, `test_api_spec_lock.py`, member/rubric/
  soft-delete suites green **unchanged**.

## Non-goals (later slices)

- **Slice 3b:** Tiptap WYSIWYG editor in the cockpit, bound to the WS sync
  server via y-prosemirror; **the actual flag-flip**; `save_section`
  git-canonical switchover; Tier-1/2 post-checkpoint re-fire.
- **Slice 3c:** awareness/presence/cursors (a new awareness channel on the sync
  protocol/room).
- Re-hydrate-room-in-place (evict blast-radius depth beyond documenting).
- Yjs-log compaction.

## Open risks / to validate in the plan

- **Component 1 in an async fn:** the synchronous `is_collab_enabled` +
  `load_rows` DB read inside `_read_artifact_impl` is consistent with the
  existing synchronous `path.read_text()`, but confirm no event-loop blocking
  concern at agent-read cadence (reads are infrequent; acceptable).
- **Section-boundary mapping (Component 2):** the fragment walk must use exactly
  the `split_sections` boundary rule so `section_index` maps identically on the
  read side (Component 1) and the apply side (Component 2). A single shared
  boundary helper is preferable to two implementations.
- **Recon-lock deadlock surface (Component 3a/3b):** the new `_recon_lock` must
  never be nested inside `Room._lock` or `manager._lock` in a way that inverts
  an existing order. Reconciliation takes `_recon_lock` then does threadpool
  resolves and brief `manager`-level mutations; document the order and assert
  deadlock-freedom (as Slice 2c did).
