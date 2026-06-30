# Collab Slice 1 — Sync Server: Implementation Notes

**Date:** 2026-06-30
**Branch:** collab-slice1-sync-server
**Tasks:** 1–7 (all merged)

---

## What shipped

- **Hand-rolled room on `pycrdt`** — zero new dependencies. The Yjs wire codec comes from `pycrdt._sync` (already in the dependency graph for Slice 0). We do not pull in `pycrdt-websocket` or any websocket room library.
- **WebSocket route** at `/api/ws/workspaces/{id}/{artifact}` (artifact ∈ `{"draft","spec"}`), mounted on the FastAPI app in `src/call_manager/web/api/ws/collab.py`.
- **Postgres `yjs_updates` log** — migration 0059 adds the `yjs_updates` table (columns: `id`, `workspace_id`, `artifact`, `actor_id`, `update_bytes`, `created_at`). Every inbound SYNC_UPDATE from an authorised writer is appended here with its actor tag.
- **Connect-time cookie auth + cached per-update write gate** — session cookie is validated once on connect via `resolve_access`; the resulting `(is_member, can_write)` tuple is closed over for the life of the connection so the DB is not hit on every frame.
- **Restart recovery by replay** — on a cold-start acquire (`RoomManager.acquire`), all `yjs_updates` rows for the `(workspace_id, artifact)` pair are loaded from Postgres in insertion order and applied to a fresh `pycrdt.Doc`. The in-memory room is then fully reconstructed without any external coordination.
- **In-process registry with evict-on-empty** — `MANAGER` (a module-global `RoomManager`) holds one `Room` per `(workspace_id, artifact)` key. When the last client disconnects, the room is evicted from the registry. The next connection rebuilds it from the durable log (tested by `test_collab_recovery.py`).

---

## Why hand-rolled (not `pycrdt-websocket`)

`pycrdt-websocket`'s `YRoom.on_message` / `BaseYStore.write` API has two limitations that would have required forking it anyway:

1. **No actor tagging.** We need to record `actor_id` (the authenticated user) on every persisted update for the audit trail. The `BaseYStore.write(data: bytes)` signature carries only the raw Yjs update bytes; there is no per-message metadata hook.
2. **Opaque persistence model.** `BaseYStore` is designed to be subclassed with a custom `write`/`read` interface, but the integration with the room's apply+broadcast cycle is baked into the library's async loop. Threading in a Postgres-backed store with our SQLAlchemy session factory and our apply→persist→broadcast ordering would have required monkey-patching internal state. Hand-rolling the 150-line `Room` + `RoomManager` is less risk and more readable.

---

## Security and robustness (Codex pre-implementation review findings)

All five findings from the Codex round-1 review were incorporated:

1. **Origin check (CSWSH defence)** — the route calls `check_origin(request)` before any auth or accept. It reuses the CSRF `_hostport` normaliser from `call_manager.web.csrf` to compare the `Origin` header against the configured `APP_BASE_URL`. A mismatched origin closes the socket with 403 before the cookie is ever inspected.

2. **Apply-then-persist ordering with poison/evict on persist failure** — the `Room.handle` method applies the Yjs update to the in-memory doc first (inside the per-room `asyncio.Lock`), then persists it to Postgres. If persistence fails, `Room.poisoned` is set and `PersistError` is raised; the route catches this, evicts the room via `MANAGER.evict`, and closes the socket. The next connection rebuilds from the durable log. The sender's CRDT client re-offers its update on reconnect, so no data is lost — the durable log remains the source of truth by construction.

3. **Broadcast error-isolation** — `Room._broadcast` iterates over all connected clients except the sender, catches any per-client send failure, collects the dead sockets in a list, and discards them after the loop. A broken or closed socket does not interrupt delivery to the remaining clients.

4. **Inbound frame-size bound** — `_MAX_FRAME_BYTES = 1_000_000` (1 MiB) is checked at the top of the message handler. HTTP body-size middleware does not cover WebSocket frames, so this is the only guard against a malicious client flooding the room with a multi-gigabyte update. Oversized frames close the socket with code 1009.

5. **Malformed-frame drop** — `doc.apply_update(update)` is wrapped in a try/except; a malformed update is logged and dropped (not persisted, not broadcast) without poisoning the room.

---

## Concurrency model

- **Per-room `asyncio.Lock`** serialises the apply+persist sequence. The lock is acquired before `doc.apply_update` and released after `persist(user_id, update)` returns. This prevents two concurrent writers from applying updates in a different order than they are persisted to Postgres.
- **Broadcast is outside the lock.** CRDT convergence tolerates delivery reordering across clients; holding the lock during broadcast would block all subsequent writers for the round-trip time of the slowest TCP connection.
- **Sync DB offloaded via `run_in_threadpool`.** SQLAlchemy's synchronous session is called from `asyncio.get_event_loop().run_in_executor` (Starlette's `run_in_threadpool`) so it does not block the event loop during the persist step.
- **Module-global `MANAGER`.** The `RoomManager` is a module-global singleton, which is correct under the single-instance constraint in the project CLAUDE.md. Horizontally scaling the sync server would require a shared room registry (e.g., Redis pub/sub); this is out of scope for Slice 1.

---

## Deferred items (named, with rationale)

- **Git→room hydration AND checkpoint-to-git** — on workspace open, the sync server should seed the room from the `proposal.md` git blob; on disconnect / periodic tick, the room's current doc should be committed back as markdown. Deferred to Slice 2: this requires the `WorkspaceRepo` git abstraction and a `markdown_bridge.pm_to_markdown` call in the server path.

- **Mid-session permission downgrade / notify** — if an owner changes a member's role or the workspace phase transitions (e.g., from active to locked) while a member is connected, the member's `can_write` flag for the live socket is stale. Deferred to Slice 2: requires a server-push notification channel (or periodic re-check) that is not needed for the Slice 1 correctness baseline.

- **Awareness / presence / cursors** — the route currently drops `AWARENESS` frames silently. Deferred to Slice 3 alongside the Tiptap frontend, which is the only consumer of awareness.

- **Tiptap frontend** — the browser-side collaborative editor (Tiptap + `y-prosemirror` + `y-websocket`) is Slice 3. Slice 1 is backend-only; the existing non-collaborative CM6 editor remains live for the draft tab.

- **Write-path Node validation (spike finding)** — the spike identified that the Python bridge's `pm_json_to_ydoc` → real browser `y-prosemirror` read is the correctness claim that matters for production. Formal validation of the bridge's write path against a running browser is deferred to Slice 2 (the first git-hydration consumer). The current golden-file tests (`test_fidelity_byte_identical_raw`) validate the read path only.

- **Yjs update-log compaction / squash** — the `yjs_updates` table is append-only. For long-lived workspaces a large number of small updates will accumulate, making cold-start hydration increasingly expensive. A periodic compaction job (squash all updates into one snapshot + truncate the log) is a follow-up optimisation deferred until the table shows measurable growth in production.

- **Full poison-broadcast (disconnect all clients of a poisoned room)** — when a room is poisoned by a persist failure, only the offending socket is closed. Other connected clients continue to send updates to the now-orphaned in-memory doc, which will be evicted only when they all disconnect. New connections already rebuild cleanly from the durable log, so no data is permanently lost. Proactively disconnecting all clients of a poisoned room is deferred as a follow-up (this is a rare DB-failure path; the operational impact is that existing clients see stale state until they reconnect naturally).
