# Collab Slice 3b-4 — Awareness / Presence / Remote Cursors (Design)

**Date:** 2026-07-04
**Status:** design approved (5 round-1 amendments + 4 round-2 review amendments folded). Round-2: **R2-1** late-identity `updateUser` effect (no editor rebuild) — §7; **R2-2** robust id attribution via `read_awareness_client_ids` payload parse ∪ states-diff (same-clientID reconnect) — §5.1/§5.2; **R2-3** provider clears remote presence on socket close (poison-evict is not client-true) — §5.4/§6; **R2-4** `_broadcast` scrubs a hard-failed client's presence (else it leaks into new-joiner snapshots) — §5.4. All grounded against real pycrdt 0.14.0 + the Tiptap v3 registry.
**Builds on:** Slice 3b-3 (Tiptap frontend + custom thin Yjs provider), merged `16e4bca` + remediation `46f5959`. Continues the collab-editing foundation ([[project_collab_editing_foundation]]).

---

## 1. Summary & scope

Add **live presence** to the collaborative Tiptap editor: remote users' **carets**, **selection highlights**, and **name + color labels**, over the existing hand-rolled pycrdt WS sync server and the custom thin Yjs provider. This is the y-awareness half of the Yjs protocol — the server currently *ignores* inbound `AWARENESS` frames ([room.py:164](../../../src/call_manager/proposal_assist/collab/room.py)) and the provider neither sends nor applies them ([collabProvider.ts:127](../../../frontend/src/collab/collabProvider.ts)).

**Scope (user-confirmed): "Cursors + selections + labels only."** Remote carets, selection highlights, and per-user name/color labels via Tiptap v3's `@tiptap/extension-collaboration-caret`.

The feature flag **stays OFF** in prod (same as 3b-1/3b-3). This slice makes presence *work* under the flag; it is not the flip (3b-5).

---

## 2. Non-goals / deferred

- **Facepile / avatar strip** ("N people here" header) — deferred to a small follow-up. Awareness carries enough to build it later with zero new transport.
- **Read-only caret when there is no selection** — a non-editable ProseMirror view only emits a caret when the user clicks into the doc; we do not synthesize a "viewing" cursor.
- **Persisted presence / history** — awareness is ephemeral, never written to `yjs_updates`, git, or any table.
- **The flag flip / CM6 cutover** — 3b-5.

---

## 3. Constraints (inherited + slice-specific)

- **Flag OFF** in prod. No room exists unless `collab_enabled` for the artifact ([collab_ws.py:82](../../../src/call_manager/proposal_assist/web/collab_ws.py)).
- **Ephemeral only** — awareness is relayed and held in memory; **never persisted**, never checkpointed, **no migration, no schema change**.
- **Backend changes additive / read-only** — the only DB-facing change is exposing an already-stored column (`User.name`) on `/api/me`.
- **CM6 flag-off parity is a hard gate** — the CodeMirror path stays byte-for-byte; `can_edit` semantics unchanged; `tests/proposal_assist/proposals/test_accepting.py`, `tests/test_api_spec_lock.py`, `tests/test_api_spec.py`, member/rubric/soft-delete suites stay green.
- **Pinned versions** (write-path-validated): `yjs@13.6.31`, `y-prosemirror@1.3.7`, `y-protocols@1.0.7`, `lib0@0.2.117`, Tiptap suite `@3.27.1`, `pycrdt 0.14.0`. New frontend dep pinned to the Tiptap line: **`@tiptap/extension-collaboration-caret@3.27.1`**.
- **Test hygiene:** pytest with `EMBEDDING_MODEL=bge-m3`, grouped-dir (never whole-suite), `-p no:cacheprovider`, and **include `tests/test_api_spec.py`** (the 3b-3 C1 lesson — a required response field has multiple construction sites). CI runs `ruff check .` (not `ruff format`). Explicit-path `git add` only.
- **pnpm fresh add** needs `--config.minimumReleaseAge=0` (the 7-day cooldown), then verify integrity vs registry.

---

## 4. Wire protocol additions

The existing sync framing is unchanged. Awareness adds one message type alongside `SYNC`.

- **Frame:** `[YMessageType.AWARENESS (1)] [varuint len] [awareness-update payload]` — exactly the format already documented in [protocol.py:9](../../../src/call_manager/proposal_assist/collab/protocol.py). On the client this is `encoding.writeVarUint(enc, MESSAGE_AWARENESS)` + `encoding.writeVarUint8Array(enc, encodeAwarenessUpdate(...))`; on the server `pycrdt.create_awareness_message(payload)` produces the identical bytes (verified: first byte `1`, varuint-length-prefixed payload).
- **Client message-type constant:** `MESSAGE_AWARENESS = 1` (mirrors `YMessageType.AWARENESS`), alongside the existing `MESSAGE_SYNC = 0`.
- **Ungated by `can_write` (Amendment a).** Presence is not a document mutation. Awareness frames are relayed for **every** connected member — read-only viewers both see others' carets and broadcast their own. It remains **membership-gated by the WS connection itself** (a non-member never accepts; a revoked member is closed 1008 and its presence removed — §5.4).
- **Never persisted.** No `apply_update`, no `store.append_update`, no checkpoint. The relay path is fully disjoint from the durable log.
- **Origin-gating, both directions (Amendment 4).** Inbound awareness is applied with the provider instance as origin; the local awareness-change handler sends only when `origin !== this` — the exact `this`-sentinel discipline already used for document updates ([collabProvider.ts:49-52](../../../frontend/src/collab/collabProvider.ts)). This prevents a received update from being re-broadcast (echo loop).

---

## 5. Server design

### 5.1 `protocol.py` (additive)

Re-export the pycrdt awareness surface and add one extractor, keeping room.py free of raw codec calls:

```python
from pycrdt import Awareness, create_awareness_message  # re-export

def extract_awareness_update(message: bytes) -> bytes:
    """Raw awareness payload from an AWARENESS frame: strip the 1-byte type
    header, read the varuint-prefixed payload. Caller checks is_awareness() first.
    Mirror of extract_update() (which strips the 2-byte SYNC header)."""
    return read_message(message[1:])

def read_awareness_client_ids(payload: bytes) -> list[int]:
    """Parse the client ids declared in a y-protocols awareness update, without
    mutating any Awareness. Update format (encodeAwarenessUpdate): a varuint
    count, then per client [clientID varuint][clock varuint][state varString].
    Read each clientID; skip clock + the varuint-length-prefixed state bytes.
    Used to attribute ids to a socket ROBUSTLY (see §5.2, Amendment R2-2)."""
```

`is_awareness()` already exists ([protocol.py:34](../../../src/call_manager/proposal_assist/collab/protocol.py)). `read_awareness_client_ids` is TDD'd against `Awareness.encode_awareness_update()` output (round-trip) and exercised end-to-end (against real JS `encodeAwarenessUpdate` bytes) by the Task-1 probe.

### 5.2 `room.py` — per-room `Awareness` + per-client id tracking

- **`Room.__init__`** creates `self.awareness = Awareness(self.doc)` and **immediately calls `self.awareness.set_local_state(None)`** (**Amendment 1**). Verified: a fresh `Awareness(doc)` seeds `states` with the server doc's own `client_id` (an empty `{}`), which would broadcast as a phantom cursor; `set_local_state(None)` clears it, and the server never re-adds a local state. (Defense-in-depth: snapshot encoding in §5.3 also only encodes ids currently in `states`, which by construction excludes the server's own once cleared.)
- **`ClientState` gains `awareness_ids: set[int]` (Amendment 2)**, `field(default_factory=set)`, **MUTABLE** (like `can_write`). `apply_awareness_update()` does not return affected ids, so we attribute ids to a socket by **combining two signals (Amendment R2-2)**: the **declared ids parsed from the payload** (`P.read_awareness_client_ids(payload)`) **unioned with** the states-diff (`set(self.awareness.states) - before`). Parse-from-payload is the primary signal and is robust to the **same-clientID reconnect** hazard: if a socket reconnects while its old awareness state still lingers in `self.awareness.states`, the diff yields nothing (the id already present), so a diff-only attribution would leave the new socket owning no ids and its caret un-removable at teardown. Parsing the payload attributes the id regardless. The diff remains as defense-in-depth. A socket owns exactly one stable awareness client id (its Y.Doc's `clientID`); a self-removal frame (`state=null`) still declares that id, so it too attributes correctly.
- **`Room.handle` — new `is_awareness` branch** (before the final catch-all `return`):

```python
if P.is_awareness(message):
    try:
        payload = P.extract_awareness_update(message)
    except Exception:
        logger.warning("collab.malformed_awareness", extra={...})
        return
    try:
        declared = set(P.read_awareness_client_ids(payload))   # robust to same-id reconnect
    except Exception:
        logger.warning("collab.malformed_awareness", extra={...})
        return
    async with self._lock:              # same lock as doc mutation; presence volume is tiny
        before = set(self.awareness.states)
        try:
            self.awareness.apply_awareness_update(payload, origin="client")
        except Exception:
            logger.warning("collab.malformed_awareness_update", extra={...})
            return
        state.awareness_ids |= declared | (set(self.awareness.states) - before)
    # Ungated (presence, not a mutation); NOT persisted. Relay the raw payload as-is.
    await self._broadcast(P.create_awareness_message(payload), exclude=sender)
    return
```

  The apply runs under `self._lock` so the before/after diff is atomic vs concurrent applies; broadcast happens outside the lock (existing "no slow client under the lock" rule). We relay the **raw inbound payload** re-wrapped (standard y-websocket behavior) — the server `Awareness` exists only to attribute ids and to synthesize snapshots/removals, not to re-encode relayed traffic.

- **`Room.awareness_snapshot() -> bytes | None`** — under `self._lock`, if `self.awareness.states` is non-empty, return `create_awareness_message(self.awareness.encode_awareness_update(list(self.awareness.states)))`, else `None`. Used to prime a new joiner (§5.3).
- **`Room.remove_client_presence(state) -> None`** — if `state.awareness_ids`: under `self._lock` call `self.awareness.remove_awareness_states(list(ids), origin="server")` and build the removal frame via `encode_awareness_update(list(ids))` (verified: still yields a ~15-byte null-state removal after removal), then `await self._broadcast(frame, exclude=None)` outside the lock. Used by the teardown paths (§5.4).

### 5.3 `collab_ws.py` — prime a new joiner (Amendment 5, server half)

After the client is registered and the initial SYNC_STEP1 is sent ([collab_ws.py:132-134](../../../src/call_manager/proposal_assist/web/collab_ws.py)), send the current awareness snapshot so the joiner sees existing carets immediately:

```python
await websocket.send_bytes(initial)
snapshot = await room.awareness_snapshot()   # None if no presence yet
if snapshot is not None:
    await websocket.send_bytes(snapshot)
```

### 5.4 Disconnect removal — wire through the room-manager teardown paths (Amendment 3)

A leaver's caret must vanish for peers **immediately**, not after the 30 s awareness timeout. All three client-removal paths pop from `room.clients` under `MANAGER._lock`:

1. **Normal disconnect** — `finally` → `MANAGER.release` ([collab_ws.py:158](../../../src/call_manager/proposal_assist/web/collab_ws.py)).
2. **Mid-session revoke / lost membership** — `reconcile_client` → `MANAGER.drop_client` (ws deleted) or `MANAGER.release` (live workspace) ([permissions.py:124-131](../../../src/call_manager/proposal_assist/collab/permissions.py)).
3. **Poison eviction** — `MANAGER.evict` closes all sockets ([room.py:327](../../../src/call_manager/proposal_assist/collab/room.py)).

**Mechanism:** `RoomManager.release` and `RoomManager.drop_client` capture `state = room.clients.get(client)` **before** popping, and record whether the room **survives** (still has clients after the pop). Outside the manager lock — mirroring how `on_evict` already runs outside the lock — if the room survived and `state` has awareness ids, call `await room.remove_client_presence(state)`.

- Room **evicted** (last client left) → no surviving peers → skip removal (the room object is gone anyway).
- Client-empty-but-ops>0 (deferred-checkpoint case) → no peers → skip.
- `evict()` (poison) closes every socket → the next connection rebuilds from the durable log; the in-memory `Awareness` dies with the room. No **server-side** removal is possible or needed (no surviving peers on that room object). **But this is not client-true (Amendment R2-3):** each surviving *browser* provider gets a 1011 close and reconnects, and would retain the other peers' stale carets until the 30 s awareness timeout. The provider therefore clears remote presence locally on any socket close (§6); peers repopulate from the reconnect snapshot + on-open flush.

**One more removal seam (Amendment R2-4):** `Room._broadcast` already drops a client whose `send_bytes` raises (a hard socket failure), popping it directly from `room.clients`. Because that pop happens *before* the WS route's `finally`→`release` runs, `release`/`drop_client` would find `removed_state=None` and never remove the dead client's awareness — so its id would linger in `room.awareness` and **leak into every future `awareness_snapshot()`**. `_broadcast`'s dead-client cleanup therefore also scrubs the dropped client's `awareness_ids` from `room.awareness` (synchronous, between awaits → atomic; no lock needed). This rare path does not re-broadcast a removal to survivors (that would be re-entrant inside `_broadcast`); their stale carets clear on the ~30 s y-awareness timeout, while the snapshot is corrected immediately.

This keeps presence-removal on the **room-manager teardown paths**, not merely on a normal WS close — so a revoke (path 2) and a normal disconnect (path 1) both drop the caret. It also composes with Slice-2c: a revoked member is closed 1008 by `reconcile_client`, and the subsequent `drop_client`/`release` removes its presence.

---

## 6. Client provider design (`collabProvider.ts`)

Add a `y-protocols/awareness` `Awareness` bound to the **same** `Y.Doc`, exposed as a public field for `CollaborationCaret`.

- **Constructor:** `this.awareness = new Awareness(this.doc)`. Register `this.awareness.on("update", this.onAwarenessUpdate)`.
- **Local awareness send (origin-gated — Amendment 4):**
  ```ts
  private onAwarenessUpdate = (changes: {added:number[];updated:number[];removed:number[]}, origin: unknown) => {
    if (origin === this) return;                    // applied FROM socket — never echo
    const ids = [...changes.added, ...changes.updated, ...changes.removed];
    const enc = encoding.createEncoder();
    encoding.writeVarUint(enc, MESSAGE_AWARENESS);
    encoding.writeVarUint8Array(enc, awarenessProtocol.encodeAwarenessUpdate(this.awareness, ids));
    this.send(encoding.toUint8Array(enc));
  };
  ```
  Note: awareness send is **ungated by `canWrite`** — unlike document updates, a read-only client broadcasts its presence.
- **Inbound apply** (replaces the current `if (messageType !== MESSAGE_SYNC) return;` blanket ignore at [collabProvider.ts:127](../../../frontend/src/collab/collabProvider.ts)): on `MESSAGE_AWARENESS`, `awarenessProtocol.applyAwarenessUpdate(this.awareness, decoding.readVarUint8Array(dec), this)` — origin `this` so the resulting `update` event is not re-broadcast. Ungated read.
- **On-open flush (Amendment 5, client half):** in `ws.onopen`, after the client STEP1, if `this.awareness.getLocalState()` is non-null, send `encodeAwarenessUpdate(this.awareness, [this.doc.clientID])`. `CollaborationCaret` may set the local user state before the socket opens (the effect that builds the provider vs the caret extension mounting), and `send()` is a no-op while `readyState !== OPEN`; without this flush the first caret would not appear until the user next moves the selection.
- **On socket close / offline (Amendment R2-3):** in `ws.onclose` (any code — 1006/1011/1008/1009), locally clear **remote** presence: `removeAwarenessStates(this.awareness, [...this.awareness.getStates().keys()].filter(id => id !== this.doc.clientID), this)` with origin `this` (so `onAwarenessUpdate` skips it — no send attempt on a dead socket, no echo). While offline the remote carets are stale (we receive no updates), so dropping them is honest; on reconnect the server's snapshot (§5.3) + peers' on-open flushes (§6) repopulate the live set. The **local** state is retained (it is re-sent by the on-open flush).
- **On destroy:** `awarenessProtocol.removeAwarenessStates(this.awareness, [this.doc.clientID], "provider-destroy")` and send the resulting removal frame (so peers drop our caret on unmount/navigate), then remove the listener and `this.awareness.destroy()`. Ordered before `this.ws.close()` so the frame is flushed.
- **Post-destroy guard** already exists on `onmessage` ([collabProvider.ts:119](../../../frontend/src/collab/collabProvider.ts)); the awareness branch lives inside the same guarded/`try` block.

The `Awareness` is rebuilt per mount along with the doc/provider (StrictMode-safe lifecycle from 3b-3, [useCollabDoc.ts](../../../frontend/src/collab/useCollabDoc.ts) — unchanged).

---

## 7. Editor integration (`CollabEditor.tsx`)

`CollaborationCaret` (Tiptap v3; the v2 `@tiptap/extension-collaboration-cursor` dead-ends at 2.x) renders remote carets + selections + labels as ProseMirror **decorations** (not schema nodes/marks — so the schema-equality drift guard is unaffected; §10).

In `CollabEditorInner`, read the current user from `useSession()` and add the extension to **both** writer and read-only editors (read-only viewers see others' carets and broadcast their own — ungated):

```ts
import { CollaborationCaret } from "@tiptap/extension-collaboration-caret";
// ...
const { data: me } = useSession();
const label = displayName(me?.user);                 // §8 (falls back until me resolves)
const color = colorForUserId(me?.user?.id ?? 0);     // §8
const editor = useEditor({
  editable: props.canWrite,
  extensions: [
    ...collabEditorExtensions,
    Collaboration.configure({ document: props.doc, field: "prosemirror" }),
    // seed with whatever identity we have now; corrected by the effect below
    CollaborationCaret.configure({ provider: props.provider, user: { name: label, color } }),
  ],
}, [props.doc]);   // editor identity keyed ONLY on the doc — NOT rebuilt on identity change

// Identity-freeze fix (Amendment R2-1): useEditor's dep list is [props.doc], so a
// useSession() result that arrives AFTER mount would otherwise freeze the fallback
// name/color for the whole mount. Push identity through the sanctioned v3 command
// whenever it resolves/changes, instead of rebuilding the editor (which would
// destroy the caret plugin state).
useEffect(() => {
  if (editor && me?.user) editor.commands.updateUser({ name: label, color });
}, [editor, label, color, me?.user]);
```

`CollaborationCaret` reads `provider.awareness` (confirmed from the v3 docs — it accepts a Yjs network provider), which our thin provider now exposes. **Peer dep note:** the caret extension declares a peer on `@tiptap/y-tiptap`; verify at install that it resolves compatibly with the pinned `y-prosemirror@1.3.7`/`yjs@13.6.31` (it is expected to already be present transitively via `@tiptap/extension-collaboration@3.27.1`). This is one of the Task-1 install-time checks.

---

## 8. Identity & color (Amendment b)

- **Backend (additive):** add `name: str | None` to `UserOut` in [me.py:15](../../../src/call_manager/web/api/me.py); `get_me` returns `user.name`. `User.name` is already stored and already surfaced by `/api/me/settings`, so this is a low-risk widening of an existing model. Regenerate `frontend/src/api/schema.gen.ts` (OpenAPI) so `MeOut.user.name` types through — regen with `EMBEDDING_MODEL=bge-m3` (the codegen stdout gotcha).
- **Label:** `displayName(user)` = `user.name || user.email.split("@")[0]`. **Clamp the label length in the UI** (e.g. 32 chars) and treat awareness identity as **display-only / advisory** — a client can spoof its own awareness payload, so the label is never an authorization signal (it is not; membership is enforced at the WS boundary).
- **Color:** `colorForUserId(id: number): string` — a **pure, deterministic** util (hash the id → hue → fixed-saturation/lightness HSL) so a user's color is stable across sessions and consistent for all peers without any server round-trip. Small unit test pins determinism + valid CSS output.

---

## 9. Backend read-model change list

| File | Change | Type |
|------|--------|------|
| [me.py](../../../src/call_manager/web/api/me.py) | `UserOut.name: str \| None`; `get_me` passes `user.name` | additive |
| `frontend/src/api/schema.gen.ts` | OpenAPI regen (`MeOut.user.name`) | generated |

No other backend model changes. `SpecOut`/`DraftOut` are untouched this slice.

---

## 10. Testing

**Server unit** (`tests/proposal_assist/collab/…` — cockpit/collab tests live under `tests/proposal_assist/`):
- **Relay:** an `AWARENESS` frame from client A is broadcast to B, sender excluded; **no `yjs_updates` row is written** (assert the store is untouched); relayed even when the sender's `ClientState.can_write is False` (ungated).
- **Id attribution + disconnect removal:** after A sends awareness, `A.state.awareness_ids` is non-empty; after `release`/`drop_client` removes A while B remains, B receives a removal frame and `room.awareness.states` no longer contains A's id.
- **Same-clientID reconnect attribution (Amendment R2-2):** a socket presents an awareness update whose clientID is already in `room.awareness.states` (simulating a reconnect before the old state expired); assert the new socket's `state.awareness_ids` still contains that id (parse-from-payload wins where the diff yields nothing), so its later teardown removes it. Plus a direct `read_awareness_client_ids` round-trip test vs `encode_awareness_update`.
- **New-joiner snapshot:** with A present (state set), a freshly-registered B receives `awareness_snapshot()` bytes after the initial sync.
- **Server-own-id suppression (Amendment 1):** a fresh `Room`'s `awareness.states` is empty; `awareness_snapshot()` of a presence-free room is `None`.
- **Revoke path (Amendment 3):** `reconcile_client` losing membership closes A 1008 and its presence is removed for B (drives `drop_client`/`release`).
- **protocol.py:** `extract_awareness_update` round-trips a `create_awareness_message`; malformed awareness payload is dropped (logged), not raised.

**Provider unit** (`frontend/src/collab/…test.ts`, jsdom):
- Local awareness change → an `AWARENESS` frame is sent; an inbound frame applied via origin `this` does **not** echo.
- Inbound `AWARENESS` mutates `provider.awareness.states`.
- On-open flush sends the local state once the socket opens.
- **Clear-remote-on-close (Amendment R2-3):** with a remote id present in `provider.awareness`, a socket `close` event drops all non-local ids locally (origin `this`, no send), while the local state is retained.
- **Late-identity update (Amendment R2-1):** a `useSession()` result arriving after mount drives `editor.commands.updateUser` (component test asserting the caret user reflects the resolved name/color, not the fallback) — the editor is not rebuilt.
- `destroy()` sends a removal frame then tears down.
- A `canWrite:false` provider **still** sends and receives awareness (ungated), while still suppressing document writes (regression guard vs the 3b-3 write-suppression).

**Live handshake probe — the Task-1 GO/NO-GO gate** (extend [handshake.probe.test.ts](../../../frontend/src/collab/handshake.probe.test.ts), Node env, real uvicorn + testcontainers PG): two `ws` clients — **A sets awareness (a user state), B sees A in `B`'s `awareness.states`; A disconnects, B sees A removed.** This is the one true unknown: JS `y-protocols/awareness` ⇄ Python `pycrdt` awareness byte-interop, and that a caret renders/clears. The straight-line extension of the 3b-3 probe the reviewer asked for.

**E2E** (`collab.spec.ts`, authored + deferred-run like 3b-3): two browsers, A types, B sees A's caret + name label. Live run deferred (harness, not product — same `:5173`-contention reason as 3b-3).

**Parity / flag-off:**
- Flag OFF ⇒ no room, no awareness (unchanged connect-close on `!collab_enabled`).
- CM6 path byte-for-byte unchanged; `can_edit` unchanged.
- **Schema-equality drift guard unchanged & still green** — `CollaborationCaret` adds decorations, not schema.
- `MeOut.name` additive; no migration; sync/persistence path unchanged; `tests/test_api_spec.py` green.

---

## 11. Task-1 de-risk (GO/NO-GO)

**Verified already (Python side, this design pass):** `set_local_state(None)` clears the server's own id; `create_awareness_message` framing (`[1][len][payload]`); `apply_awareness_update` + states-diff attribution; `encode_awareness_update` snapshot; `remove_awareness_states` + re-encode removal frame.

**Not yet proven — Task 1 must show GO before any further task:** the **cross-runtime interop** — that `pycrdt.create_awareness_message`/`encode_awareness_update` bytes are consumed by JS `awarenessProtocol.applyAwarenessUpdate` (and vice-versa), end to end through the real WS server, and that a disconnect removal actually clears the remote entry. If NO-GO (byte-format mismatch between pycrdt and y-protocols), fall back to a hand-rolled awareness codec on the Python side matched to y-protocols — but the `is_awareness_disconnect_message`/`create_awareness_message` surface strongly suggests pycrdt targets the y-protocols format, so GO is expected.

---

## 12. File-by-file change list

**Server (additive, no schema, no migration):**
- `proposal_assist/collab/protocol.py` — re-export `Awareness`, `create_awareness_message`; add `extract_awareness_update` + `read_awareness_client_ids`.
- `proposal_assist/collab/room.py` — `Room.awareness` (+`set_local_state(None)`), `ClientState.awareness_ids`, `handle()` awareness branch, `awareness_snapshot()`, `remove_client_presence()`; `RoomManager.release`/`drop_client` presence-removal hook.
- `proposal_assist/web/collab_ws.py` — send `awareness_snapshot()` after the initial sync.
- `web/api/me.py` — `UserOut.name`.

**Frontend:**
- `frontend/package.json` — add `@tiptap/extension-collaboration-caret@3.27.1`.
- `frontend/src/collab/collabProvider.ts` — `awareness` field, send/receive/on-open-flush/destroy-removal, `MESSAGE_AWARENESS`, origin-gating.
- `frontend/src/collab/CollabEditor.tsx` — `CollaborationCaret` + identity via `useSession()`.
- `frontend/src/collab/collabColor.ts` (new) — `colorForUserId`, `displayName`.
- `frontend/src/api/schema.gen.ts` — OpenAPI regen (`MeOut.user.name`).

**Tests:** server awareness relay/attribution/removal/snapshot/revoke + protocol extractor; provider awareness send/receive/flush/destroy/ungated; probe extension (Task-1 gate); color util; e2e (authored, deferred); parity/flag-off assertions.

---

## 13. Verification gates (reproduced, not trusted — mirrors 3b-3)

- Backend flag-off parity: `EMBEDDING_MODEL=bge-m3 pytest tests/proposal_assist/ tests/test_api_workspaces.py tests/test_api_spec.py tests/test_api_spec_lock.py tests/proposal_assist/proposals/test_accepting.py -p no:cacheprovider` → all green (baseline from 3b-3: 1655 passed).
- Frontend: `pnpm tsc` clean + full vitest suite green (baseline 498) + new awareness tests.
- **Task-1 live probe GO** (awareness interop + disconnect removal) — precondition for tasks 2+.
- CM6 empty-diff parity; schema-equality drift guard green; `ruff check .` clean.
- No new migration; no `enable_collab`/flag change in `src/`.

---

## 14. Process

- Worktree `.worktrees/collab-slice3b4` (base master at the merge SHA), real `pnpm install` (this slice adds a dep — no node_modules symlink trick), venv editable install.
- **Detached SDD**, stop-before-merge. Independent verify-before-merge: reproduce every gate + read the load-bearing diffs (transport framing, teardown-path removal, parity).
- Do **not** invoke user-billed slash reviews. `git add` explicit paths only. Never touch master/main from the worktree. Merge is **local only**; do not push without an explicit ask.
