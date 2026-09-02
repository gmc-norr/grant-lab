# Collab document epoch — a retained Y.Doc must prove which document it holds

**Date:** 2026-09-02
**Status:** approved design, unimplemented
**Closes:** review of record `docs/superpowers/reviews/2026-09-02-thorough-project-review.md`
§2.1 / Appendix A6 F1 (HIGH). The last open item of that review's §7 step 3.

## 1. The defect

A branch switch (`proposal_assist/branches.py` steps 4–6) tears the rooms down, closes
every connected socket with 4409, checkpoints the log, truncates it and resets the
watermark, all in one transaction. The client's 4409 arm
(`frontend/src/collab/collabProvider.ts`, `WS_DOCUMENT_REPLACED`) discards the Y.Doc and
`useCollabDoc` builds a fresh pair.

A client that is **not connected** at that moment never sees the 4409: it is in
reconnect backoff after a network blip (1006, up to 30 s × 12 attempts), asleep on a
half-open socket, or its close frame was buffered into a dead socket. It keeps the old
branch's Y.Doc. When it reconnects, `collab_ws.py` mints a **fresh** fence stamp (so
`WorkspaceGenerationStale` cannot fire), sends the room's STEP1, and the client answers
with a STEP2 carrying its whole old document. `Room.handle` applies it, persists it under
that user's `actor_id`, and the next checkpoint commits the doubled document to the new
branch. Reproduced from the main tree (review §2.1, `probe_stale_doc_merge.py`):

```
room before reconnect: '## Aims\n\nNew aims text.\n'
room AFTER reconnect:  '## Aims\n\nNew aims text.\n\n## Aims\n\nOld aims text.\n\n## Methods\n\nOld methods.\n'
```

Nothing on the wire identifies *which* document the client holds. The generation fence
cannot carry that identity because it is re-minted on every connect.

## 2. The design in one paragraph

Every collab artifact gets a **document epoch**: an integer in
`collab_artifact_state.doc_epoch` that increases each time the log is reset. The room
learns its epoch when it cold-starts, from the same mutex-held read as the log. The
server tells every client the epoch in the first frame after accept. A client whose doc
has synced presents that epoch on every reconnect as `?epoch=N`; the server compares it
with the room's epoch **after accept and before STEP1** and closes 4409 on mismatch, so
the stale document is never asked for its state. A fresh doc connects without an epoch
(an empty doc can merge nothing), learns the current one, and carries it from then on.

Chosen over "the draft/spec GET returns the epoch and the editor passes it in": that
needs a refetch on every 4409 and can race an uncommitted switch. Decided 2026-09-02.

## 3. Server

### 3.1 Storage

- Alembic `0069`: `ALTER TABLE collab_artifact_state ADD COLUMN doc_epoch BIGINT NOT NULL
  DEFAULT 0`. Downgrade drops it. ORM: `CollabArtifactState.doc_epoch: Mapped[int]`,
  `server_default="0"`, `default=0`.
- `state.get_doc_epoch(session, *, workspace_id, artifact) -> int`: the row's value, `0`
  when there is no row (an artifact never enabled or never checkpointed).
- `state.reset_checkpoint` (the function both truncate callers already invoke in the same
  transaction as `store.truncate_log`) **also does `row.doc_epoch += 1`** and creates the
  row if absent, so an artifact whose log is truncated before it ever had a state row
  still moves off epoch 0. Its docstring states the rule: *a truncated log is a new
  document*. `store.truncate_log`'s docstring cross-references it.

  Callers today: `branches.switch_branch` (step 6) and `handover.run_kill_switch_handover`.
  A test pins each one: after the operation, `get_doc_epoch` is one higher for every
  artifact it truncated.

### 3.2 The room carries its epoch

- `hydrate.load_or_hydrate` returns `Hydration`, a `list[bytes]` subclass with one extra
  attribute, `doc_epoch: int`, read by `state.get_doc_epoch` in the **same session, after
  `acquire_commit_mutex_at_generation`** — so the epoch and the log rows are one
  consistent snapshot. Every existing caller iterates or indexes the result and keeps
  working unchanged.
- `Room.doc_epoch: int = 0`. `RoomManager.acquire` and `RoomManager.operate` set
  `room.doc_epoch = getattr(updates, "doc_epoch", 0)` at their cold-start sites (the two
  `for update in await load()` loops, `room.py:558` and `:608`). A warm join uses the
  room's value. Fake loaders in tests that return a plain `[]` give epoch 0.
- A room never outlives an epoch change: both callers of `reset_checkpoint` tear rooms
  down first (`branches.py` step 4; the kill switch disables the artifact, which the
  route refuses with 1008). `MANAGER.evict` on a poisoned room forces a fresh cold start.

### 3.3 The route (`web/collab_ws.py`)

1. Parse `epoch` from the query string before accept. Absent → `None`. Present but not a
   non-negative integer → close 1008 (policy), like any other malformed request.
2. Unchanged: origin, auth, membership, flag, accept, `MANAGER.acquire` and its existing
   except arms.
3. **First statement inside the `try` that owns `finally: release`** (so a refused socket
   is deregistered): if `client_epoch is not None and client_epoch != room.doc_epoch`,
   log `collab.stale_doc_refused` (INFO, workspace, artifact, client epoch, room epoch),
   close `WS_DOCUMENT_REPLACED` (4409), return. No STEP1 has been sent; no STEP2 can
   arrive.
4. After `reconcile_client` and the straddle re-validation, and **before** the STEP1:
   send `protocol.create_doc_epoch_message(room.doc_epoch)`.

The existing `WorkspaceGenerationStale → 4409` arm stays. It covers the millisecond in
which a switch commits between this connect's authorization and its cold-start hydrate;
the epoch covers every reconnect after the switch.

### 3.4 Wire format (`collab/protocol.py`)

- `MESSAGE_DOC_EPOCH = 4`. y-protocols uses 0 sync, 1 awareness, 2 auth, 3 query-awareness;
  4 is free on both sides.
- Frame: `[varuint 4] [varuint epoch]`, built by `create_doc_epoch_message(epoch: int)`
  with pycrdt's `write_var_uint`.
- Inbound: `Room.handle` already ignores every type it does not know ("Anything else:
  ignored"), so a client that echoes the frame changes nothing. A test pins that.

## 4. Client (`frontend/src/collab/`)

- `collabProvider.ts`: `MESSAGE_DOC_EPOCH = 4`; a private `docEpoch: number | null = null`,
  exposed read-only as `provider.docEpoch`. `onmessage`: a frame of type 4 sets it and
  returns (it is neither sync nor awareness). `connect()` opens
  `this.url` when `docEpoch === null`, else `this.url + "?epoch=" + docEpoch`.
- Nothing else changes. On 4409 the provider still never reconnects; `useCollabDoc` still
  destroys the pair and builds a fresh one, which connects without an epoch and learns
  the new one. The replacement cap and backoff (spec 2026-09-01 bug 2) are untouched.
- A page reload is a fresh doc: no persistence of the epoch, by design.

## 5. Compatibility

| Client \ Server | old server | new server |
|---|---|---|
| old client | today | never receives frame 4 (ignores unknown types), never sends `?epoch` → accepted as today, unprotected until it reloads |
| new client | never receives frame 4 → `docEpoch` stays `null` → today | protected |

No REST or OpenAPI change. `docs/ARCHITECTURE.md` is regenerated (new column).

## 6. Tests

Backend (`tests/proposal_assist/collab/`):

1. **The review's reproduction, refused.** Open a room with content, capture its epoch;
   run the switch's truncate+reset (or `reset_checkpoint` directly) and re-open the room
   with new content; connect a socket presenting the old epoch: the first thing it
   receives is a close with code 4409 and **no STEP1 frame**; the room's markdown is
   unchanged. (`test_collab_ws_route.py`, real `TestClient.websocket_connect`.)
2. A socket presenting the current epoch receives frame 4 then STEP1 and can persist.
3. A socket presenting no epoch receives frame 4 (with the room's epoch) then STEP1.
4. `?epoch=abc` and `?epoch=-1` close 1008 before any frame.
5. `reset_checkpoint` increments `doc_epoch` and creates a missing row; `switch_branch`
   and `run_kill_switch_handover` each leave every artifact they truncated one epoch
   higher (extend `test_branch_switch.py` and `test_collab_kill_switch_handover.py`).
6. `load_or_hydrate` returns a `Hydration` whose `doc_epoch` matches the row, on both the
   warm (rows exist) and cold-seed paths; `RoomManager.acquire` and `.operate` record it
   on the room; a plain-list loader yields 0.
7. `create_doc_epoch_message` round-trips through the bounded varuint reader;
   `Room.handle` drops an inbound frame 4.
8. Mutation proof at authoring time (per `measure-dont-predict`): each of the route
   check, the frame send, the `reset_checkpoint` bump and the manager's recording is
   disabled in turn and its test must fail.

Frontend (`collabProvider.test.ts`, `FakeSocket`):

9. After a frame 4 carrying 7, `provider.docEpoch === 7` and the next reconnect (drop
   1006) opens `…/draft?epoch=7`; the first connect opened the bare URL.
10. A frame 4 does not set `synced` and is not echoed.
11. After 4409 the provider stays down (unchanged test) and a NEW provider opens the bare
    URL.

## 7. Out of scope

- Persisting the epoch across page reloads.
- Any change to the 4409 replacement cap, the reconnect backoff, or the draft/spec GET.
- The epoch is per artifact; it is not the workspace lifecycle generation and does not
  replace the fence stamp.
