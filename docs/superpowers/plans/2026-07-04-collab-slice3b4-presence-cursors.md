# Collab Slice 3b-4 — Awareness / Presence / Remote Cursors Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add live presence (remote carets + selections + name/color labels) to the collaborative Tiptap editor, over the existing hand-rolled pycrdt WS server and the custom thin Yjs provider.

**Architecture:** Server maintains a per-room `pycrdt.Awareness`; it relays inbound `AWARENESS` frames to peers (ungated, never persisted), attributes each frame's client ids to the sending socket, primes new joiners with a snapshot, and broadcasts a removal when a client leaves (on every room-manager teardown path). The thin provider gains a `y-protocols/awareness` `Awareness`: it sends local changes, applies inbound ones (origin-gated), flushes on open, clears remote presence on close, and removes its own state on destroy. Tiptap v3's `CollaborationCaret` renders the carets from `provider.awareness`; identity (name/color) comes from `/api/me` (new additive `UserOut.name`) and is corrected via `editor.commands.updateUser` when it resolves.

**Tech Stack:** Python 3.12 / FastAPI / pycrdt 0.14.0; React 19 / Vite / Tiptap v3 (`@tiptap/*@3.27.1`) / `@tiptap/extension-collaboration-caret@3.27.1` (NEW) / `y-protocols@1.0.7` / `yjs@13.6.31` / `lib0@0.2.117`.

**Spec:** `docs/superpowers/specs/2026-07-04-collab-slice3b4-presence-cursors-design.md`.

## Global Constraints

- **Feature flag STAYS OFF in prod.** No room exists unless `collab_enabled` for the artifact. This slice does not flip anything.
- **Awareness is ephemeral:** relayed + in-memory only. **Never persisted** (no `apply_update`, no `store.append_update`, no checkpoint). **No migration, no schema change.**
- **Backend changes additive / read-only.** The only DB-facing change is exposing the already-stored `User.name` on `/api/me`.
- **CM6 flag-off parity is a hard gate.** CodeMirror path byte-for-byte unchanged; `can_edit` semantics unchanged. These stay green: `tests/proposal_assist/proposals/test_accepting.py`, `tests/test_api_spec_lock.py`, `tests/test_api_spec.py`, member/rubric/soft-delete suites.
- **Pinned versions:** `yjs@13.6.31`, `y-prosemirror@1.3.7`, `y-protocols@1.0.7`, `lib0@0.2.117`, Tiptap suite `@3.27.1`, `pycrdt 0.14.0`. New dep pinned: `@tiptap/extension-collaboration-caret@3.27.1`.
- **Awareness is UNGATED by `can_write`** (presence, not a mutation) but membership-gated by the WS connection. Origin-gated both directions (the `this`-sentinel already used for doc updates).
- **Test hygiene:** pytest with `EMBEDDING_MODEL=bge-m3`, grouped-dir (never whole-suite), `-p no:cacheprovider`. Always include `tests/test_api_spec.py` in the backend sweep. CI runs `ruff check .` (not `ruff format`). `git add` explicit paths only.
- **pnpm fresh add** needs `--config.minimumReleaseAge=0` (7-day cooldown); verify integrity vs registry after.
- **Wire frame:** `[YMessageType.AWARENESS (1)] [varuint len] [payload]`. Client const `MESSAGE_AWARENESS = 1`.
- **Process:** worktree `.worktrees/collab-slice3b4`, real `pnpm install` (this slice adds a dep). Detached SDD, **stop before merge**. Never touch master/main from the worktree. Merge is local only; do not push without an explicit ask.

## File Structure

**Server (additive; no schema; no migration):**
- `src/call_manager/proposal_assist/collab/protocol.py` — re-export `Awareness`, `create_awareness_message`; add `extract_awareness_update`, `read_awareness_client_ids`. (Task 1)
- `src/call_manager/proposal_assist/collab/room.py` — `Room.awareness` (+`set_local_state(None)`), `ClientState.awareness_ids`, `handle()` awareness branch, `awareness_snapshot()`, `remove_client_presence()`; `RoomManager.release`/`drop_client` presence-removal hook. (Tasks 2, 5, 6)
- `src/call_manager/proposal_assist/web/collab_ws.py` — send `awareness_snapshot()` after the initial sync. (Task 5)
- `src/call_manager/web/api/me.py` — `UserOut.name`. (Task 9)

**Frontend:**
- `frontend/src/collab/collabProvider.ts` — `awareness` field, send/receive/on-open-flush/destroy-removal/clear-on-close, `MESSAGE_AWARENESS`, origin-gating. (Tasks 3, 8)
- `frontend/src/collab/collabColor.ts` (new) — `colorForUserId`, `displayName`. (Task 10)
- `frontend/src/collab/CollabEditor.tsx` — `CollaborationCaret` + identity via `useSession()` + `updateUser` effect. (Task 11)
- `frontend/package.json` — add `@tiptap/extension-collaboration-caret@3.27.1`. (Task 11)
- `frontend/src/api/schema.gen.ts` — OpenAPI regen (`MeOut.user.name`). (Task 9)

**Tests:**
- `tests/proposal_assist/collab/test_collab_protocol.py` (extend) — Task 1
- `tests/proposal_assist/collab/test_collab_awareness.py` (new) — Tasks 2, 5, 6
- `frontend/src/collab/collabProvider.test.ts` (extend) — Tasks 3, 8
- `frontend/src/collab/handshake.probe.test.ts` (extend) — Tasks 4, 7 (LIVE GATES)
- `frontend/src/collab/collabColor.test.ts` (new) — Task 10
- `frontend/src/collab/CollabEditor.identity.test.tsx` (new) — Task 11
- `tests/test_api_me.py` (extend) — Task 9
- `frontend/e2e/collab.spec.ts` (extend, authored/deferred) — Task 12

**Gate ordering note:** The one true unknown is JS `y-protocols/awareness` ⇄ Python `pycrdt` **byte-interop** (everything Python-side is already proven in the design). It can only be exercised live once the minimal relay + provider path exists, so the GO/NO-GO gate is split: **Task 4** (convergence: A sets → B sees) is the earliest possible live proof; **Task 7** (removal: A leaves → B sees removal) completes it after the teardown wiring lands. Tasks 1–3 and 5–6 are unit-tested so they are independently sound even before the live gates.

---

### Task 1: Awareness protocol helpers

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/protocol.py`
- Test: `tests/proposal_assist/collab/test_collab_protocol.py`

**Interfaces:**
- Produces: `extract_awareness_update(message: bytes) -> bytes`; `read_awareness_client_ids(payload: bytes) -> list[int]`; re-exported `Awareness`, `create_awareness_message`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/proposal_assist/collab/test_collab_protocol.py`:

```python
from pycrdt import Doc as _Doc  # if not already imported at top
from call_manager.proposal_assist.collab import protocol as P


def _awareness_payload(user):
    d = _Doc()
    a = P.Awareness(d)
    a.set_local_state(user)
    return a.encode_awareness_update([a.client_id]), a.client_id


def test_extract_awareness_update_roundtrips_create_awareness_message():
    payload, _ = _awareness_payload({"user": {"name": "Alice"}})
    frame = P.create_awareness_message(payload)
    assert P.is_awareness(frame)
    assert P.extract_awareness_update(frame) == payload


def test_read_awareness_client_ids_single():
    payload, cid = _awareness_payload({"user": {"name": "Alice", "color": "#f00"}})
    assert P.read_awareness_client_ids(payload) == [cid]


def test_read_awareness_client_ids_multi_and_removal():
    # Merge two clients into one server awareness, encode a multi-client snapshot.
    d = _Doc()
    srv = P.Awareness(d)
    srv.set_local_state(None)
    p1, c1 = _awareness_payload({"user": {"name": "A"}})
    p2, c2 = _awareness_payload({"user": {"name": "B"}})
    srv.apply_awareness_update(p1, "x")
    srv.apply_awareness_update(p2, "x")
    snap = srv.encode_awareness_update(list(srv.states))
    assert sorted(P.read_awareness_client_ids(snap)) == sorted([c1, c2])
    # A removal frame (state=null) STILL declares the id.
    srv.remove_awareness_states([c1], "srv")
    rem = srv.encode_awareness_update([c1])
    assert P.read_awareness_client_ids(rem) == [c1]
```

- [ ] **Step 2: Run to verify they fail**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_protocol.py -p no:cacheprovider -q`
Expected: FAIL — `AttributeError: module ... has no attribute 'Awareness'` / `extract_awareness_update`.

- [ ] **Step 3: Implement in `protocol.py`**

Extend the pycrdt import and `__all__`, then add the two helpers:

```python
from pycrdt import (
    Awareness,
    YMessageType,
    YSyncMessageType,
    create_awareness_message,
    create_sync_message,
    create_update_message,
    handle_sync_message,
    read_message,
)

__all__ = [
    "create_sync_message",
    "handle_sync_message",
    "create_update_message",
    "create_awareness_message",
    "Awareness",
    "is_sync_step1",
    "is_sync_write",
    "is_awareness",
    "extract_update",
    "extract_awareness_update",
    "read_awareness_client_ids",
]


def extract_awareness_update(message: bytes) -> bytes:
    """Raw awareness payload from an AWARENESS frame: strip the 1-byte type header,
    read the varuint-prefixed payload. Caller checks is_awareness() first.
    Mirror of extract_update() (which strips the 2-byte SYNC header)."""
    return read_message(message[1:])


def read_awareness_client_ids(payload: bytes) -> list[int]:
    """Parse the client ids declared in a y-protocols awareness update WITHOUT
    mutating any Awareness. Update format (encodeAwarenessUpdate): a varuint
    count, then per client [clientID varuint][clock varuint][state varString].
    Read each clientID; skip clock + the varuint-length-prefixed state bytes."""
    pos = 0

    def _rvu() -> int:
        nonlocal pos
        num = 0
        shift = 0
        while True:
            b = payload[pos]
            pos += 1
            num |= (b & 0x7F) << shift
            if b < 0x80:
                return num
            shift += 7

    count = _rvu()
    ids: list[int] = []
    for _ in range(count):
        ids.append(_rvu())   # clientID
        _rvu()               # clock (skip)
        state_len = _rvu()   # varString byte length
        pos += state_len     # skip state bytes
    return ids
```

- [ ] **Step 4: Run to verify pass**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_protocol.py -p no:cacheprovider -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/collab/protocol.py tests/proposal_assist/collab/test_collab_protocol.py
git commit -m "feat(collab): awareness protocol helpers (extract + client-id parse)"
```

---

### Task 2: Server awareness relay (`room.py`)

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/room.py`
- Test: `tests/proposal_assist/collab/test_collab_awareness.py` (new)

**Interfaces:**
- Consumes: `P.is_awareness`, `P.extract_awareness_update`, `P.read_awareness_client_ids`, `P.create_awareness_message`, `P.Awareness` (Task 1).
- Produces: `Room.awareness: Awareness`; `ClientState.awareness_ids: set[int]`; `Room.handle()` relays AWARENESS frames (ungated, not persisted, attributes ids).

- [ ] **Step 1: Write the failing tests** (`tests/proposal_assist/collab/test_collab_awareness.py`)

```python
import uuid

import pytest
from pycrdt import Awareness, Doc

from call_manager.proposal_assist.collab import protocol as P
from call_manager.proposal_assist.collab.room import ClientState, Room


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeClient:
    def __init__(self):
        self.sent: list[bytes] = []
        self.closed: int | None = None

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000) -> None:
        self.closed = code


def _awareness_frame(user):
    d = Doc()
    a = Awareness(d)
    a.set_local_state(user)
    frame = P.create_awareness_message(a.encode_awareness_update([a.client_id]))
    return frame, a.client_id


async def _noop_persist(user_id, update):
    raise AssertionError("awareness must never be persisted")


@pytest.mark.anyio
async def test_awareness_relayed_to_others_not_persisted_not_to_sender():
    room = Room((uuid.uuid4(), "draft"))
    sender, other = FakeClient(), FakeClient()
    st = ClientState(user_id=7, can_write=True)
    room.clients[sender] = st
    room.clients[other] = ClientState(user_id=1, can_write=True)
    frame, cid = _awareness_frame({"user": {"name": "Alice"}})

    await room.handle(sender=sender, state=st, message=frame, persist=_noop_persist)

    assert other.sent and not sender.sent          # relayed to peer, not echoed
    assert P.is_awareness(other.sent[-1])
    assert cid in st.awareness_ids                  # attributed to the sender socket
    assert cid in room.awareness.states


@pytest.mark.anyio
async def test_awareness_is_ungated_read_only_sender_still_relays():
    room = Room((uuid.uuid4(), "draft"))
    reader, other = FakeClient(), FakeClient()
    st = ClientState(user_id=9, can_write=False)     # read-only
    room.clients[reader] = st
    room.clients[other] = ClientState(user_id=1, can_write=True)
    frame, cid = _awareness_frame({"user": {"name": "ReadOnly"}})

    await room.handle(sender=reader, state=st, message=frame, persist=_noop_persist)

    assert other.sent                                # presence relayed even for a non-writer
    assert cid in st.awareness_ids


@pytest.mark.anyio
async def test_same_clientid_reconnect_is_attributed_via_payload_parse():
    # A client id already present in room.awareness (its old state lingers); a NEW
    # socket sends an update for the SAME id. The states-diff yields nothing, so
    # attribution must come from parsing the payload (R2-2).
    room = Room((uuid.uuid4(), "draft"))
    first, second, other = FakeClient(), FakeClient(), FakeClient()
    frame, cid = _awareness_frame({"user": {"name": "Ghost"}})
    st1 = ClientState(user_id=1, can_write=True)
    room.clients[first] = st1
    room.clients[other] = ClientState(user_id=2, can_write=True)
    await room.handle(sender=first, state=st1, message=frame, persist=_noop_persist)
    assert cid in room.awareness.states              # old state lingers

    st2 = ClientState(user_id=1, can_write=True)      # reconnect, same doc/client id
    room.clients[second] = st2
    await room.handle(sender=second, state=st2, message=frame, persist=_noop_persist)
    assert cid in st2.awareness_ids                   # attributed despite empty diff


@pytest.mark.anyio
async def test_malformed_awareness_dropped_without_raising():
    room = Room((uuid.uuid4(), "draft"))
    sender, other = FakeClient(), FakeClient()
    st = ClientState(user_id=1, can_write=True)
    room.clients[sender] = st
    room.clients[other] = ClientState(user_id=2, can_write=True)
    bad = bytes([1, 0xFF, 0xFF, 0xFF])                # AWARENESS type + truncated payload
    await room.handle(sender=sender, state=st, message=bad, persist=_noop_persist)  # no raise
    assert other.sent == []


@pytest.mark.anyio
async def test_room_awareness_has_no_server_local_state():
    room = Room((uuid.uuid4(), "draft"))
    assert room.awareness.states == {}                # set_local_state(None) at init
```

- [ ] **Step 2: Run to verify they fail**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_awareness.py -p no:cacheprovider -q`
Expected: FAIL — `AttributeError: 'Room' object has no attribute 'awareness'` / `ClientState` has no `awareness_ids`.

- [ ] **Step 3: Implement in `room.py`**

Add `field` to the dataclass import:

```python
from dataclasses import dataclass, field
```

Extend `ClientState`:

```python
@dataclass
class ClientState:
    """... `can_write` is MUTABLE ... `awareness_ids` tracks the awareness client
    ids this socket has declared, so its presence can be removed on disconnect."""
    user_id: int
    can_write: bool
    awareness_ids: set[int] = field(default_factory=set)
```

In `Room.__init__`, after `self.doc = Doc()`:

```python
        self.doc = Doc()
        # Per-room presence. set_local_state(None) clears the server doc's own
        # awareness entry (a fresh Awareness seeds states[doc.client_id] = {}),
        # so the server never broadcasts a phantom cursor.
        self.awareness = P.Awareness(self.doc)
        self.awareness.set_local_state(None)
```

In `Room.handle`, replace the final `# AWARENESS and anything else: ignored ...` block:

```python
        if P.is_awareness(message):
            # Presence relay: ungated (not a doc mutation) and NEVER persisted.
            try:
                payload = P.extract_awareness_update(message)
                declared = set(P.read_awareness_client_ids(payload))
            except Exception:
                logger.warning(
                    "collab.malformed_awareness",
                    extra={"event": "collab_malformed_awareness", "room": str(self.key)},
                )
                return
            async with self._lock:
                before = set(self.awareness.states)
                try:
                    self.awareness.apply_awareness_update(payload, origin="client")
                except Exception:
                    logger.warning(
                        "collab.malformed_awareness_update",
                        extra={"event": "collab_malformed_awareness_update",
                               "room": str(self.key)},
                    )
                    return
                # Attribute ids to THIS socket: payload-declared ids (robust to a
                # same-clientID reconnect where the diff is empty) ∪ states-diff.
                state.awareness_ids |= declared | (set(self.awareness.states) - before)
            # Relay the raw payload as-is to peers (standard y-awareness relay).
            await self._broadcast(P.create_awareness_message(payload), exclude=sender)
            return

        # Anything else: ignored.
        return
```

- [ ] **Step 4: Run to verify pass**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_awareness.py tests/proposal_assist/collab/test_collab_room.py -p no:cacheprovider -q`
Expected: PASS (new awareness tests + existing room tests still green).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/collab/room.py tests/proposal_assist/collab/test_collab_awareness.py
git commit -m "feat(collab): server awareness relay (ungated, ephemeral) + per-socket id attribution"
```

---

### Task 3: Provider awareness send / receive / on-open flush

**Files:**
- Modify: `frontend/src/collab/collabProvider.ts`
- Test: `frontend/src/collab/collabProvider.test.ts`

**Interfaces:**
- Produces: `CollabProvider.awareness: awarenessProtocol.Awareness` (public, consumed by `CollaborationCaret` in Task 11). Sends `[MESSAGE_AWARENESS]` frames on local awareness change (ungated) and on open; applies inbound `AWARENESS` frames (origin `this`).

- [ ] **Step 1: Write the failing tests** (append to `collabProvider.test.ts`)

Add near the top (helpers):

```ts
import * as awarenessProtocol from "y-protocols/awareness";
const MESSAGE_AWARENESS = 1;

function firstMsgType(b: Uint8Array): number {
  return decoding.readVarUint(decoding.createDecoder(b));
}
function awarenessFrameFor(user: Record<string, unknown>): Uint8Array {
  const d = new Y.Doc();
  const aw = new awarenessProtocol.Awareness(d);
  aw.setLocalStateField("user", user);
  const e = encoding.createEncoder();
  encoding.writeVarUint(e, MESSAGE_AWARENESS);
  encoding.writeVarUint8Array(e, awarenessProtocol.encodeAwarenessUpdate(aw, [d.clientID]));
  return encoding.toUint8Array(e);
}
```

Add tests inside the `describe`:

```ts
it("sends an AWARENESS frame when the local awareness changes", () => {
  const { p, sockets } = makeProvider(true);
  const s = sockets[0]; s.open(); s.sent.length = 0;
  p.awareness.setLocalStateField("user", { name: "Alice", color: "#f00" });
  expect(s.sent).toHaveLength(1);
  expect(firstMsgType(s.sent[0])).toBe(MESSAGE_AWARENESS);
});

it("applies inbound AWARENESS to provider.awareness and does NOT echo", () => {
  const { p, sockets } = makeProvider(true);
  const s = sockets[0]; s.open(); s.sent.length = 0;
  s.deliver(awarenessFrameFor({ name: "Bob" }));
  const users = [...p.awareness.getStates().values()].map((st) => (st as any).user?.name);
  expect(users).toContain("Bob");
  expect(s.sent).toHaveLength(0);        // applied with origin `this` → not echoed
});

it("flushes local awareness on open (state set before the socket opened)", () => {
  const { p, sockets } = makeProvider(true);
  p.awareness.setLocalStateField("user", { name: "Early" });  // before open: send is a no-op
  const s = sockets[0];
  s.open();
  // open sends STEP1 + the awareness flush
  const types = s.sent.map(firstMsgType);
  expect(types).toContain(MESSAGE_AWARENESS);
});

it("read-only client still sends and receives awareness (ungated)", () => {
  const { p, sockets } = makeProvider(false);
  const s = sockets[0]; s.open(); s.sent.length = 0;
  p.awareness.setLocalStateField("user", { name: "Viewer" });
  expect(s.sent.some((b) => firstMsgType(b) === MESSAGE_AWARENESS)).toBe(true);  // sends
  s.deliver(awarenessFrameFor({ name: "Peer" }));
  const users = [...p.awareness.getStates().values()].map((st) => (st as any).user?.name);
  expect(users).toContain("Peer");                                              // receives
});

it("sends an awareness removal frame on destroy", () => {
  const { p, sockets } = makeProvider(true);
  const s = sockets[0]; s.open();
  p.awareness.setLocalStateField("user", { name: "Leaver" });
  s.sent.length = 0;
  p.destroy();
  expect(s.sent.some((b) => firstMsgType(b) === MESSAGE_AWARENESS)).toBe(true);
});
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd frontend && pnpm vitest run src/collab/collabProvider.test.ts`
Expected: FAIL — `p.awareness` is undefined; no AWARENESS frames sent.

- [ ] **Step 3: Implement in `collabProvider.ts`**

Add the import + constant:

```ts
import * as awarenessProtocol from "y-protocols/awareness";
const MESSAGE_SYNC = 0;
const MESSAGE_AWARENESS = 1;
const WS_OPEN = 1;
```

Add the field + a send helper, and build awareness in the constructor:

```ts
export class CollabProvider {
  readonly doc: Y.Doc;
  readonly awareness: awarenessProtocol.Awareness;
  // ... existing fields ...
  private readonly onAwarenessUpdate: (
    changes: { added: number[]; updated: number[]; removed: number[] },
    origin: unknown,
  ) => void;

  constructor(opts: CollabProviderOptions) {
    this.doc = opts.doc;
    this.awareness = new awarenessProtocol.Awareness(this.doc);
    // ... existing assignments ...
    this.onUpdate = (update, origin) => { /* unchanged */ };
    this.doc.on("update", this.onUpdate);
    this.onAwarenessUpdate = (changes, origin) => {
      // Origin-gated (same discipline as doc updates): never echo an update we
      // just applied FROM the socket. Awareness send is UNGATED by canWrite.
      if (origin === this) return;
      this.sendAwareness([...changes.added, ...changes.updated, ...changes.removed]);
    };
    this.awareness.on("update", this.onAwarenessUpdate);
    this.connect();
  }

  private sendAwareness(ids: number[]): void {
    if (!ids.length) return;
    const enc = encoding.createEncoder();
    encoding.writeVarUint(enc, MESSAGE_AWARENESS);
    encoding.writeVarUint8Array(enc, awarenessProtocol.encodeAwarenessUpdate(this.awareness, ids));
    this.send(encoding.toUint8Array(enc));
  }
```

In `ws.onopen`, after sending STEP1:

```ts
      this.send(encoding.toUint8Array(enc));
      // Flush our local awareness so peers/server learn it even if the caret set
      // the local user state before the socket opened (Amendment 5).
      if (this.awareness.getLocalState() !== null) {
        this.sendAwareness([this.doc.clientID]);
      }
```

In `ws.onmessage`, before the `if (messageType !== MESSAGE_SYNC) return;` line:

```ts
        const messageType = decoding.readVarUint(dec);
        if (messageType === MESSAGE_AWARENESS) {
          // Ungated read: viewers see peers' carets. origin `this` → no echo.
          awarenessProtocol.applyAwarenessUpdate(this.awareness, decoding.readVarUint8Array(dec), this);
          return;
        }
        if (messageType !== MESSAGE_SYNC) return;
```

In `destroy()` (remove the listener FIRST so the manual removal isn't double-sent by the handler):

```ts
  destroy(): void {
    this.destroyed = true;
    if (this.timer) { clearTimeout(this.timer); this.timer = null; }
    // Announce our departure so peers drop our caret immediately (before close).
    this.awareness.off("update", this.onAwarenessUpdate);
    awarenessProtocol.removeAwarenessStates(this.awareness, [this.doc.clientID], "destroy");
    this.sendAwareness([this.doc.clientID]);
    this.awareness.destroy();
    this.doc.off("update", this.onUpdate);
    if (this.ws) { try { this.ws.close(); } catch { /* already closed */ } this.ws = null; }
    this.listeners.clear();
  }
```

- [ ] **Step 4: Run to verify pass**

Run: `cd frontend && pnpm vitest run src/collab/collabProvider.test.ts && pnpm tsc --noEmit`
Expected: PASS + clean types.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/collab/collabProvider.ts frontend/src/collab/collabProvider.test.ts
git commit -m "feat(collab): provider awareness send/receive/flush (origin-gated, ungated)"
```

---

### Task 4: LIVE INTEROP GATE #1 — awareness convergence (GO/NO-GO)

**This is the de-risk gate. Do not proceed to Task 5 unless it is GO.** It proves pycrdt ⇄ y-protocols/awareness byte-interop end-to-end through the real WS server.

**Files:**
- Modify: `frontend/src/collab/handshake.probe.test.ts`
- Uses: `scripts/collab_handshake_probe.py` (existing driver: testcontainers PG + uvicorn-in-thread + seed; prints workspace id + cookie + origin).

**Interfaces:**
- Consumes: `CollabProvider.awareness` (Task 3), server relay (Task 2).

- [ ] **Step 1: Add the awareness convergence probe** (append inside the `describe` in `handshake.probe.test.ts`)

```ts
it("awareness converges: A sets presence, B sees it", async () => {
  const docA = new Y.Doc();
  const a = new CollabProvider({ url: URL, doc: docA, canWrite: true, WebSocketImpl: WSWithAuth });
  const docB = new Y.Doc();
  const b = new CollabProvider({ url: URL, doc: docB, canWrite: true, WebSocketImpl: WSWithAuth });
  await settle();
  a.awareness.setLocalStateField("user", { name: "Alice", color: "#ff0000" });
  await settle();
  const names = [...b.awareness.getStates().values()].map((s: any) => s?.user?.name);
  expect(names).toContain("Alice");           // GO: JS↔Python awareness interop works
  a.destroy(); b.destroy();
}, 15000);
```

- [ ] **Step 2: Run the live probe**

```bash
cd frontend
# Terminal-in-one: the driver seeds + starts uvicorn, prints env, then runs vitest.
# (Follows the 3b-3 recipe; the driver enables collab for the seeded workspace.)
EMBEDDING_MODEL=bge-m3 uv run python ../scripts/collab_handshake_probe.py --run-vitest
# or, if the driver only prints env: export PROBE_WS_URL/PROBE_COOKIE/PROBE_ORIGIN then:
#   pnpm vitest run --config vitest.probe.config.ts src/collab/handshake.probe.test.ts
```
Expected: the new test PASSES (plus the existing convergence/persistence probe).

- [ ] **Step 3: GO/NO-GO decision**

- **GO** → the awareness wire is proven; continue to Task 5.
- **NO-GO** (byte-format mismatch) → STOP and report. Fallback per spec §11: replace the pycrdt awareness encode/decode with a hand-rolled codec matched to y-protocols on the Python side; the unit tests (Tasks 1–3) still hold. Re-run this gate.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/collab/handshake.probe.test.ts
git commit -m "test(collab): live awareness-convergence probe (interop GATE #1 = GO)"
```

---

### Task 5: New-joiner awareness snapshot

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/room.py`, `src/call_manager/proposal_assist/web/collab_ws.py`
- Test: `tests/proposal_assist/collab/test_collab_awareness.py`

**Interfaces:**
- Produces: `async Room.awareness_snapshot() -> bytes | None`; `collab_ws` sends it after the initial sync.

- [ ] **Step 1: Write the failing test** (append to `test_collab_awareness.py`)

```python
@pytest.mark.anyio
async def test_awareness_snapshot_primes_a_new_joiner():
    room = Room((uuid.uuid4(), "draft"))
    a = FakeClient()
    st = ClientState(user_id=1, can_write=True)
    room.clients[a] = st
    frame, cid = _awareness_frame({"user": {"name": "Present"}})
    await room.handle(sender=a, state=st, message=frame, persist=_noop_persist)

    snapshot = await room.awareness_snapshot()
    assert snapshot is not None and P.is_awareness(snapshot)
    # a fresh awareness applying the snapshot sees the present user
    joiner = Awareness(Doc())
    joiner.set_local_state(None)
    joiner.apply_awareness_update(P.extract_awareness_update(snapshot), "srv")
    assert cid in joiner.states


@pytest.mark.anyio
async def test_awareness_snapshot_is_none_when_empty():
    room = Room((uuid.uuid4(), "draft"))
    assert await room.awareness_snapshot() is None
```

- [ ] **Step 2: Run to verify fail**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_awareness.py -p no:cacheprovider -q`
Expected: FAIL — `Room` has no `awareness_snapshot`.

- [ ] **Step 3: Implement**

In `room.py`, add to `Room` (e.g. after `initial_message`):

```python
    async def awareness_snapshot(self) -> bytes | None:
        """The current presence as an AWARENESS wire frame, to prime a joining
        client. None if no presence yet. Excludes the server's own id (cleared
        at init)."""
        async with self._lock:
            ids = list(self.awareness.states)
            if not ids:
                return None
            return P.create_awareness_message(self.awareness.encode_awareness_update(ids))
```

In `collab_ws.py`, after `await websocket.send_bytes(initial)`:

```python
        await websocket.send_bytes(initial)
        snapshot = await room.awareness_snapshot()
        if snapshot is not None:
            await websocket.send_bytes(snapshot)
```

- [ ] **Step 4: Run to verify pass**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/ -p no:cacheprovider -q`
Expected: PASS (awareness + ws-route suites green).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/collab/room.py src/call_manager/proposal_assist/web/collab_ws.py tests/proposal_assist/collab/test_collab_awareness.py
git commit -m "feat(collab): prime new joiners with an awareness snapshot"
```

---

### Task 6: Disconnect removal on the room-manager teardown paths

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/room.py`
- Test: `tests/proposal_assist/collab/test_collab_awareness.py`

**Interfaces:**
- Produces: `async Room.remove_client_presence(state)`; `RoomManager.release` / `RoomManager.drop_client` broadcast a removal when a client leaves and the room survives.

- [ ] **Step 1: Write the failing tests** (append to `test_collab_awareness.py`)

```python
from call_manager.proposal_assist.collab.room import RoomManager


@pytest.mark.anyio
async def test_release_removes_departed_presence_for_survivors():
    mgr = RoomManager()
    key = (uuid.uuid4(), "draft")
    a, b = FakeClient(), FakeClient()

    async def _load():
        return []

    sta = ClientState(user_id=1, can_write=True)
    await mgr.acquire(key, load=_load, client=a, state=sta)
    await mgr.acquire(key, load=_load, client=b, state=ClientState(user_id=2, can_write=True))
    room = mgr.get(key)
    frame, cid = _awareness_frame({"user": {"name": "Alice"}})
    await room.handle(sender=a, state=sta, message=frame, persist=_noop_persist)
    b.sent.clear()

    await mgr.release(key, client=a)                 # A leaves, B remains
    assert b.sent and P.is_awareness(b.sent[-1])     # B told to drop A's caret
    assert cid not in room.awareness.states


@pytest.mark.anyio
async def test_drop_client_removes_departed_presence_for_survivors():
    mgr = RoomManager()
    key = (uuid.uuid4(), "draft")
    a, b = FakeClient(), FakeClient()

    async def _load():
        return []

    sta = ClientState(user_id=1, can_write=True)
    await mgr.acquire(key, load=_load, client=a, state=sta)
    await mgr.acquire(key, load=_load, client=b, state=ClientState(user_id=2, can_write=True))
    room = mgr.get(key)
    frame, cid = _awareness_frame({"user": {"name": "Bob"}})
    await room.handle(sender=a, state=sta, message=frame, persist=_noop_persist)
    b.sent.clear()

    await mgr.drop_client(key, a)                     # revoke path
    assert b.sent and P.is_awareness(b.sent[-1])
    assert cid not in room.awareness.states


@pytest.mark.anyio
async def test_release_last_client_no_presence_broadcast_no_crash():
    mgr = RoomManager()
    key = (uuid.uuid4(), "draft")
    a = FakeClient()

    async def _load():
        return []

    sta = ClientState(user_id=1, can_write=True)
    await mgr.acquire(key, load=_load, client=a, state=sta)
    room = mgr.get(key)
    frame, _ = _awareness_frame({"user": {"name": "Solo"}})
    await room.handle(sender=a, state=sta, message=frame, persist=_noop_persist)
    await mgr.release(key, client=a)                   # last client → room evicted, no survivor
    assert not mgr.has_room(key)
```

- [ ] **Step 2: Run to verify fail**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_awareness.py -p no:cacheprovider -q`
Expected: FAIL — `Room` has no `remove_client_presence`; `b.sent` empty after release.

- [ ] **Step 3: Implement in `room.py`**

Add to `Room`:

```python
    async def remove_client_presence(self, state: ClientState) -> None:
        """Remove a departed client's awareness ids and broadcast the removal to
        remaining clients so their carets drop immediately (not after the ~30s
        timeout). No-op if the client owned no presence. Callers invoke this only
        when the room still has peers (see RoomManager.release/drop_client)."""
        ids = list(state.awareness_ids)
        if not ids:
            return
        async with self._lock:
            self.awareness.remove_awareness_states(ids, origin="server")
            frame = P.create_awareness_message(self.awareness.encode_awareness_update(ids))
        await self._broadcast(frame, exclude=None)
```

Modify `RoomManager.release`:

```python
    async def release(self, key: RoomKey, *, client, on_evict=None) -> None:
        removed_state = None
        survivor = None
        async with self._lock:
            room = self._rooms.get(key)
            if room is None:
                return
            removed_state = room.clients.get(client)
            room.clients.pop(client, None)
            if not room.clients and room.ops == 0:
                evicted = self._rooms.pop(key)
            else:
                evicted = None
                if room.clients:
                    survivor = room     # peers remain → drop the leaver's caret
                elif on_evict is not None:
                    room._pending_on_evict = on_evict
        # Presence removal + checkpoint both run OUTSIDE the manager lock.
        if survivor is not None and removed_state is not None:
            await survivor.remove_client_presence(removed_state)
        if evicted is not None and on_evict is not None:
            await on_evict(evicted)
```

Modify `RoomManager.drop_client`:

```python
    async def drop_client(self, key: RoomKey, client) -> None:
        removed_state = None
        survivor = None
        async with self._lock:
            room = self._rooms.get(key)
            if room is None:
                return
            removed_state = room.clients.get(client)
            room.clients.pop(client, None)
            if not room.clients and room.ops == 0 and self._rooms.get(key) is room:
                self._rooms.pop(key, None)
            elif room.clients:
                survivor = room
        if survivor is not None and removed_state is not None:
            await survivor.remove_client_presence(removed_state)
```

- [ ] **Step 4: Run to verify pass**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/ -p no:cacheprovider -q`
Expected: PASS (awareness + room + ws-route + operate + evict-tombstone suites green — release/drop_client behavior preserved).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/collab/room.py tests/proposal_assist/collab/test_collab_awareness.py
git commit -m "feat(collab): broadcast presence removal on release/drop_client teardown"
```

---

### Task 7: LIVE GATE #2 — awareness removal on disconnect

**Files:**
- Modify: `frontend/src/collab/handshake.probe.test.ts`

- [ ] **Step 1: Add the removal probe** (append inside the `describe`)

```ts
it("awareness removal: A disconnects, B stops seeing A", async () => {
  const docA = new Y.Doc();
  const a = new CollabProvider({ url: URL, doc: docA, canWrite: true, WebSocketImpl: WSWithAuth });
  const docB = new Y.Doc();
  const b = new CollabProvider({ url: URL, doc: docB, canWrite: true, WebSocketImpl: WSWithAuth });
  await settle();
  a.awareness.setLocalStateField("user", { name: "Alice", color: "#f00" });
  await settle();
  expect([...b.awareness.getStates().values()].some((s: any) => s?.user?.name === "Alice")).toBe(true);
  a.destroy();                 // sends removal + server release broadcasts removal
  await settle();
  expect([...b.awareness.getStates().values()].some((s: any) => s?.user?.name === "Alice")).toBe(false);
  b.destroy();
}, 15000);
```

- [ ] **Step 2: Run the live probe**

Run (same driver as Task 4):
`cd frontend && EMBEDDING_MODEL=bge-m3 uv run python ../scripts/collab_handshake_probe.py --run-vitest`
Expected: both awareness probes PASS. **GO** completes the full interop gate (convergence + removal).

- [ ] **Step 3: Commit**

```bash
git add frontend/src/collab/handshake.probe.test.ts
git commit -m "test(collab): live awareness-removal probe (interop GATE #2 = GO)"
```

---

### Task 8: Provider clears remote presence on socket close (R2-3)

**Files:**
- Modify: `frontend/src/collab/collabProvider.ts`
- Test: `frontend/src/collab/collabProvider.test.ts`

**Interfaces:**
- Behavior: on any socket close, drop all non-local awareness ids locally (origin `this`, no send). Local state retained; peers repopulate from the reconnect snapshot + on-open flush.

- [ ] **Step 1: Write the failing test** (append inside the `describe`)

```ts
it("clears REMOTE awareness (keeps local) when the socket closes", () => {
  const { p, doc, sockets } = makeProvider(true);
  const s = sockets[0]; s.open();
  p.awareness.setLocalStateField("user", { name: "Me" });     // local state
  s.deliver(awarenessFrameFor({ name: "Peer" }));             // a remote peer
  expect([...p.awareness.getStates().keys()].length).toBeGreaterThanOrEqual(2);
  s.drop(1011);                                                // server error → reconnecting
  const ids = [...p.awareness.getStates().keys()];
  expect(ids).toEqual([doc.clientID]);                         // only local remains
});
```

- [ ] **Step 2: Run to verify fail**

Run: `cd frontend && pnpm vitest run src/collab/collabProvider.test.ts`
Expected: FAIL — the remote peer state survives the close.

- [ ] **Step 3: Implement in `ws.onclose`** (move the destroyed-guard above the clear, then clear remote)

```ts
    ws.onclose = (ev: CloseEvent) => {
      this.ws = null;
      if (this.destroyed) return;   // destroy() already tore down awareness
      // While disconnected we receive no awareness updates, so remote carets are
      // stale. Drop all NON-local ids locally (origin `this`: no send, no echo).
      // On reconnect the server snapshot + peers' on-open flush repopulate. (R2-3)
      const remote = [...this.awareness.getStates().keys()].filter((id) => id !== this.doc.clientID);
      if (remote.length) awarenessProtocol.removeAwarenessStates(this.awareness, remote, this);
      if (ev.code === 1008 || ev.code === 1009) { this.setStatus("closed-policy"); return; }
      this.setStatus("offline");
      this.scheduleReconnect();
    };
```

- [ ] **Step 4: Run to verify pass**

Run: `cd frontend && pnpm vitest run src/collab/collabProvider.test.ts && pnpm tsc --noEmit`
Expected: PASS + clean types.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/collab/collabProvider.ts frontend/src/collab/collabProvider.test.ts
git commit -m "feat(collab): provider clears remote presence on socket close (R2-3)"
```

---

### Task 9: Expose `name` on `/api/me` + regenerate OpenAPI types

**Files:**
- Modify: `src/call_manager/web/api/me.py`
- Modify (generated): `frontend/src/api/schema.gen.ts`
- Test: `tests/test_api_me.py`

**Interfaces:**
- Produces: `MeOut.user.name: str | None` (frontend consumes it in Task 11).

- [ ] **Step 1: Write the failing test** (append to `tests/test_api_me.py`, matching its existing client/fixtures)

```python
def test_me_exposes_user_name(client, make_user):
    # Adapt to the file's existing helpers: create a user with a name, log in,
    # GET /api/me, assert the name surfaces.
    user = make_user(email="named@ki.se", name="Dr. Named")
    with logged_in(client, user):
        body = client.get("/api/me").json()
    assert body["user"]["name"] == "Dr. Named"


def test_me_name_is_null_when_unset(client, make_user):
    user = make_user(email="anon@ki.se", name=None)
    with logged_in(client, user):
        body = client.get("/api/me").json()
    assert body["user"]["name"] is None
```

> If `tests/test_api_me.py` uses different fixture names, mirror the file's existing login/user-creation idiom (do not invent `make_user`/`logged_in` if the file names them otherwise — read the top of the file first).

- [ ] **Step 2: Run to verify fail**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/test_api_me.py -p no:cacheprovider -q`
Expected: FAIL — `KeyError: 'name'` (field absent from `UserOut`).

- [ ] **Step 3: Implement in `me.py`**

```python
class UserOut(BaseModel):
    id: int
    email: str
    name: str | None
    status: str
    default_profile_id: int | None
```

```python
    return MeOut(
        user=UserOut(
            id=user.id,
            email=user.email,
            name=user.name,
            status=user.status,
            default_profile_id=user.default_profile_id,
        ),
        capabilities={"admin": user.is_admin},
    )
```

- [ ] **Step 4: Run to verify pass + regenerate types**

```bash
EMBEDDING_MODEL=bge-m3 uv run pytest tests/test_api_me.py -p no:cacheprovider -q   # PASS
cd frontend && pnpm gen:api                                                        # regen schema.gen.ts (env pinned inside the script)
git diff --stat src/api/schema.gen.ts                                              # expect MeOut.user.name added
pnpm tsc --noEmit                                                                  # types still clean
```
Expected: test PASS; `schema.gen.ts` gains `name` under the `/api/me` user object.

- [ ] **Step 5: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor
git add src/call_manager/web/api/me.py frontend/src/api/schema.gen.ts tests/test_api_me.py
git commit -m "feat(api): expose user name on /api/me (additive) + regen OpenAPI types"
```

---

### Task 10: Deterministic color + display-name util

**Files:**
- Create: `frontend/src/collab/collabColor.ts`
- Test: `frontend/src/collab/collabColor.test.ts`

**Interfaces:**
- Produces: `colorForUserId(id: number): string` (stable HSL); `displayName(user?: { name?: string | null; email?: string }): string` (`name || email-localpart`, clamped, safe fallback).

- [ ] **Step 1: Write the failing tests** (`collabColor.test.ts`)

```ts
import { describe, expect, it } from "vitest";
import { colorForUserId, displayName } from "./collabColor";

describe("colorForUserId", () => {
  it("is deterministic and a valid hsl()", () => {
    const c = colorForUserId(42);
    expect(c).toMatch(/^hsl\(\d+(\.\d+)? \d+% \d+%\)$/);
    expect(colorForUserId(42)).toBe(c);
  });
  it("varies across ids", () => {
    expect(colorForUserId(1)).not.toBe(colorForUserId(2));
  });
});

describe("displayName", () => {
  it("prefers name, falls back to email local-part", () => {
    expect(displayName({ name: "Dr. Who", email: "who@ki.se" })).toBe("Dr. Who");
    expect(displayName({ name: null, email: "who@ki.se" })).toBe("who");
  });
  it("has a safe fallback when the user is unresolved", () => {
    expect(displayName(undefined)).toBe("Anonymous");
  });
  it("clamps very long labels", () => {
    expect(displayName({ name: "x".repeat(100), email: "x@ki.se" }).length).toBeLessThanOrEqual(32);
  });
});
```

- [ ] **Step 2: Run to verify fail**

Run: `cd frontend && pnpm vitest run src/collab/collabColor.test.ts`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `collabColor.ts`**

```ts
// Deterministic per-user presence identity. Awareness identity is display-only
// and client-spoofable — never an authorization signal (membership is enforced
// at the WS boundary).

const MAX_LABEL = 32;

export function colorForUserId(id: number): string {
  // Stable hue from the id; fixed saturation/lightness for legible carets.
  let h = 2166136261 ^ Math.trunc(id);
  h = Math.imul(h, 16777619) >>> 0;
  const hue = h % 360;
  return `hsl(${hue} 70% 45%)`;
}

export function displayName(user?: { name?: string | null; email?: string }): string {
  const raw = user?.name?.trim() || user?.email?.split("@")[0] || "Anonymous";
  return raw.length > MAX_LABEL ? raw.slice(0, MAX_LABEL) : raw;
}
```

- [ ] **Step 4: Run to verify pass**

Run: `cd frontend && pnpm vitest run src/collab/collabColor.test.ts && pnpm tsc --noEmit`
Expected: PASS + clean types.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/collab/collabColor.ts frontend/src/collab/collabColor.test.ts
git commit -m "feat(collab): deterministic per-user caret color + display-name util"
```

---

### Task 11: CollaborationCaret integration + identity (with late-resolve fix R2-1)

**Files:**
- Modify: `frontend/package.json` (add dep), `frontend/src/collab/CollabEditor.tsx`
- Test: `frontend/src/collab/CollabEditor.identity.test.tsx` (new)

**Interfaces:**
- Consumes: `CollabProvider.awareness` (Task 3), `colorForUserId`/`displayName` (Task 10), `useSession` (`MeOut.user.name` from Task 9).

- [ ] **Step 1: Add the dependency**

```bash
cd frontend
pnpm add @tiptap/extension-collaboration-caret@3.27.1 --config.minimumReleaseAge=0
# verify the resolved version + integrity, and that @tiptap/y-tiptap resolved compatibly
pnpm ls @tiptap/extension-collaboration-caret @tiptap/y-tiptap y-prosemirror
```
Expected: `@tiptap/extension-collaboration-caret 3.27.1`; `@tiptap/y-tiptap` present (transitive under `@tiptap/extension-collaboration`); `y-prosemirror 1.3.7` unchanged.

- [ ] **Step 2: Write the failing test** (`CollabEditor.identity.test.tsx`)

This asserts the R2-1 late-identity fix: a `useSession()` result arriving after mount is pushed into `provider.awareness` (via `editor.commands.updateUser`, which writes the `user` field to awareness local state) — the editor is not rebuilt.

```tsx
import { render, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

// Mock useSession so we control when identity resolves.
const sessionData: { value: any } = { value: undefined };
vi.mock("../api/session", () => ({ useSession: () => ({ data: sessionData.value }) }));

// Use a FakeSocket so no real network is needed (mirror collabProvider.test.ts).
vi.mock("./useCollabDoc", async () => {
  const Y = await import("yjs");
  const { CollabProvider } = await import("./collabProvider");
  return {
    useCollabDoc: (opts: any) => {
      const doc = new Y.Doc();
      // minimal fake socket provider
      class FakeSocket { static OPEN=1; readyState=1; bufferedAmount=0; binaryType="arraybuffer";
        onopen:any; onmessage:any; onclose:any; onerror:any; constructor(public url:string){ queueMicrotask(()=>this.onopen?.()); }
        send(){} close(){ this.onclose?.({code:1000}); } }
      const provider = new CollabProvider({ url:"ws://x/api/ws/workspaces/w/draft", doc, canWrite:opts.canWrite, WebSocketImpl: FakeSocket as any });
      return { doc, provider, status: "connected" };
    },
  };
});

import { CollabEditor } from "./CollabEditor";
import { renderWithProviders } from "../test/renderWithProviders"; // use the repo's helper

describe("CollabEditor identity", () => {
  it("updates the caret user when useSession resolves after mount (R2-1)", async () => {
    sessionData.value = undefined;                 // unresolved at mount
    const utils = renderWithProviders(<CollabEditor workspaceId="w" artifact="draft" canWrite />);
    // resolve identity late
    sessionData.value = { user: { id: 7, name: "Late Name", email: "late@ki.se" } };
    utils.rerender(<CollabEditor workspaceId="w" artifact="draft" canWrite />);
    await waitFor(() => {
      // the awareness local state's user reflects the resolved identity
      const anyUser = document.querySelector('[data-testid="collab-editor"]');
      expect(anyUser).toBeTruthy();
    });
    // NOTE: assert on provider.awareness.getLocalState().user via a test hook if the
    // harness exposes the provider; otherwise this render-smoke + the e2e (Task 12)
    // cover the visual. Keep this test lightweight — the pure identity mapping is
    // unit-tested in Task 10.
  });
});
```

> Testing a live Tiptap editor + caret plugin in jsdom is fragile (see 3b-3's StrictMode/mock experience). Keep this test to a render-smoke that proves the wiring compiles and mounts with the late-resolve rerender; the **authoritative** identity proof is the pure util test (Task 10) + the e2e (Task 12). If the harness exposes the provider, strengthen the assertion to `provider.awareness.getLocalState()?.user.name === "Late Name"`.

- [ ] **Step 3: Run to verify fail**

Run: `cd frontend && pnpm vitest run src/collab/CollabEditor.identity.test.tsx`
Expected: FAIL — `CollaborationCaret`/identity not wired yet (or import error).

- [ ] **Step 4: Implement in `CollabEditor.tsx`**

```tsx
import Collaboration from "@tiptap/extension-collaboration";
import { CollaborationCaret } from "@tiptap/extension-collaboration-caret";
import { EditorContent, useEditor } from "@tiptap/react";
import { useEffect } from "react";
import type * as Y from "yjs";
import { useSession } from "../api/session";
import { resetEditorStore } from "../cockpit/editorStore";
import { collabEditorExtensions } from "./CitationNode";
import { colorForUserId, displayName } from "./collabColor";
import type { CollabProvider, ProviderStatus } from "./collabProvider";
import { SaveVersionButton } from "./SaveVersionButton";
import { useCollabDoc } from "./useCollabDoc";
```

In `CollabEditorInner`:

```tsx
function CollabEditorInner(
  props: CollabEditorProps & { doc: Y.Doc; provider: CollabProvider; status: ProviderStatus },
) {
  const { data: me } = useSession();
  const label = displayName(me?.user);
  const color = colorForUserId(me?.user?.id ?? 0);
  const editor = useEditor(
    {
      editable: props.canWrite,
      extensions: [
        ...collabEditorExtensions,
        Collaboration.configure({ document: props.doc, field: "prosemirror" }),
        // Seed with whatever identity we have now; the effect below corrects it
        // if useSession() resolves after mount (R2-1). Added for writers AND
        // read-only viewers (they see peers' carets and broadcast their own).
        CollaborationCaret.configure({ provider: props.provider, user: { name: label, color } }),
      ],
    },
    [props.doc], // editor keyed ONLY on the doc — NOT rebuilt on identity change
  );

  // R2-1: useEditor's dep list is [props.doc], so a late useSession() result would
  // freeze the fallback name/color. Push identity through the sanctioned v3 command
  // whenever it resolves/changes, instead of rebuilding the editor (which would
  // destroy the caret plugin state).
  useEffect(() => {
    if (editor && me?.user) editor.commands.updateUser({ name: label, color });
  }, [editor, label, color, me?.user]);

  return (
    <div data-collab-status={props.status}>
      <SaveVersionButton
        id={props.workspaceId}
        artifact={props.artifact}
        provider={props.provider}
        canWrite={props.canWrite}
        status={props.status}
      />
      <EditorContent editor={editor} data-testid="collab-editor" />
    </div>
  );
}
```

- [ ] **Step 5: Run to verify pass**

Run: `cd frontend && pnpm vitest run src/collab/CollabEditor.identity.test.tsx && pnpm tsc --noEmit`
Expected: PASS + clean types. Also run the schema drift guard to confirm the caret adds no schema: `pnpm vitest run src/collab/collabSchema.equality.test.ts` → PASS.

- [ ] **Step 6: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor
git add frontend/package.json frontend/pnpm-lock.yaml frontend/src/collab/CollabEditor.tsx frontend/src/collab/CollabEditor.identity.test.tsx
git commit -m "feat(collab): render remote carets via CollaborationCaret + late-resolve identity (R2-1)"
```

---

### Task 12: E2E (authored, deferred-run) + final parity/verification sweep

**Files:**
- Modify: `frontend/e2e/collab.spec.ts` (authored; env-gated skip like 3b-3)

- [ ] **Step 1: Author the presence e2e** (env-gated skip; not run in CI)

```ts
// Extends the 3b-3 collab.spec.ts. Runs only when E2E_COLLAB_WS_ID + cookie are set.
test("two browsers: A types, B sees A's caret and name label", async ({ browser }) => {
  // ... open two contexts on the same workspace/artifact (reuse the 3b-3 seed) ...
  // A focuses the editor and types; assert B's DOM shows a remote caret widget
  // with A's label. Selector: the CollaborationCaret decoration renders a
  // `.collaboration-carets__caret` / `.collaboration-carets__label` (v3 class
  // names — confirm against the installed package's CSS on first run).
});
```

Note in the spec file header: live run deferred (harness, not product — same `:5173`-contention reason as 3b-3); transport already proven by the Task 4/7 live gates.

- [ ] **Step 2: Full backend flag-off parity sweep**

```bash
EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/ tests/test_api_workspaces.py tests/test_api_spec.py \
  tests/test_api_spec_lock.py tests/test_api_me.py \
  tests/proposal_assist/proposals/test_accepting.py \
  -p no:cacheprovider -q > /tmp/3b4_backend.log 2>&1; echo PYTEST_EXIT=$?
```
Expected: `PYTEST_EXIT=0`. (Baseline from 3b-3 was 1655 passed; this adds the awareness tests.)

- [ ] **Step 3: Full frontend sweep**

```bash
cd frontend
pnpm tsc --noEmit
pnpm vitest run                      # full unit suite (excludes the live probe config)
pnpm exec ruff --version >/dev/null 2>&1 || true
```
Expected: tsc clean; all unit tests green (baseline 498 + new awareness/color/identity tests).

- [ ] **Step 4: Lint**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run ruff check .
```
Expected: clean.

- [ ] **Step 5: Parity assertions (manual verification checklist)**

- [ ] No migration added (`ls src/call_manager/db/migrations/versions | wc -l` unchanged vs base).
- [ ] No `enable_collab` / flag flip anywhere in `src/` diff.
- [ ] CM6 path untouched: `git diff master -- frontend/src/cockpit/ArtifactSectionEditor*` is empty (byte-for-byte).
- [ ] `SpecOut`/`DraftOut` unchanged this slice (grep the diff — only `me.py` UserOut changed).

- [ ] **Step 6: Commit**

```bash
git add frontend/e2e/collab.spec.ts
git commit -m "test(collab): author presence e2e (deferred-run) + slice verification notes"
```

---

## Self-Review

**Spec coverage** (each spec section → task):
- §4 wire protocol (frame, MESSAGE_AWARENESS, ungated, not persisted, origin-gated) → Tasks 1–3.
- §5.1 protocol helpers → Task 1. §5.2 relay + attribution (incl R2-2) → Task 2. §5.3 snapshot → Task 5. §5.4 removal on teardown (Amdt 3) → Task 6.
- §6 provider (send/receive/flush Amdt 5, destroy removal, clear-on-close R2-3) → Tasks 3, 8.
- §7 CollaborationCaret + updateUser effect (R2-1) → Task 11.
- §8 identity (UserOut.name Amdt b, displayName, colorForUserId) → Tasks 9, 10.
- §10 testing (server unit, provider unit, probe gate, e2e, parity) → every task + Task 12.
- §11 Task-1 de-risk (interop GO/NO-GO) → Tasks 4 (convergence) + 7 (removal).
- Amendment 1 (server phantom) → Task 2 `set_local_state(None)` + `test_room_awareness_has_no_server_local_state`.

**Placeholder scan:** every code step shows real code; every test shows real assertions; commands include the exact `EMBEDDING_MODEL=bge-m3 … -p no:cacheprovider` form. The only softened items are (a) the `test_api_me.py` fixture names (flagged: mirror the file's idiom) and (b) the jsdom caret component test (flagged: render-smoke + Task-10 unit + Task-12 e2e are the authoritative proof) — both intentional, with the concrete fallback named.

**Type/name consistency:** `awareness_ids` (snake) server-side; `awareness` field + `sendAwareness`/`onAwarenessUpdate` client-side; `MESSAGE_AWARENESS = 1` both sides; `colorForUserId`/`displayName` defined in Task 10 and consumed in Task 11; `awareness_snapshot()`/`remove_client_presence()` defined in Tasks 5/6 and used in Task 5's `collab_ws` + Task 6's `RoomManager`. `updateUser` is the Tiptap v3 CollaborationCaret command.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-04-collab-slice3b4-presence-cursors.md`.

Per the standing process for this project: **detached SDD** (subagent-driven-development) in the worktree `.worktrees/collab-slice3b4`, **stop before merge**, followed by an independent verify-before-merge (reproduce the Task 4/7 live gates + the parity sweep + read the load-bearing diffs: transport framing, teardown-path removal, CM6 empty-diff).
