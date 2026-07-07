# src/call_manager/proposal_assist/collab/room.py
"""In-process Yjs rooms (socket-agnostic).

A Room owns one pycrdt.Doc, the set of connected clients, and an asyncio lock
serialising mutations. It is deliberately free of FastAPI, sockets, and the DB:
clients are any object with `async send_bytes(bytes)`, and persistence/recovery
are injected callables. The WS route (Task 6) supplies real sockets and a
Postgres-backed persist/load.

Single-instance constraint: the module-global RoomManager IS the room registry;
all clients of a room reach the same process, so in-memory state is canonical
for the live doc and Postgres is the durable replay log.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field

from pycrdt import Doc

from . import protocol as P

RoomKey = tuple[uuid.UUID, str]
PersistFn = Callable[[int, bytes], Awaitable[None]]
LoadFn = Callable[[], Awaitable[list[bytes]]]

# An empty Yjs update is two zero bytes; nothing to apply/persist/broadcast.
_EMPTY_UPDATE = b"\x00\x00"

# RFC 6455 close code: server error (used for poison-room eviction)
_WS_INTERNAL_ERROR = 1011

# Per-USER cap on awareness client-id bindings per room (Slice 3b-5d.D2). Legit
# usage is ~1 binding per page-load, so 64 is generous. Per-user, NOT per-room:
# a room-wide cap would be an abuse amplifier — one member could exhaust it and
# every future joiner's first-announce would be whole-dropped (presence-silent
# for the room's life: attack #1's effect achieved THROUGH the mitigation). A
# per-user quota confines a spammer to exhausting only its own presence; total
# map growth is bounded by members × quota.
_MAX_AWARENESS_BINDINGS_PER_USER = 64


logger = logging.getLogger(__name__)


class PersistError(Exception):
    """Durable persistence of an applied update failed.

    The in-memory doc is now AHEAD of the durable log, so the room is poisoned
    and must be evicted and rebuilt from the log (the route does this). The
    sender's CRDT client re-offers its update on reconnect, so no data is lost.
    """


@dataclass
class ClientState:
    """Per-connection state the room tracks for a client socket. `can_write` is
    MUTABLE: mid-session permission reconciliation (Slice 2c) flips it in place,
    and the WS route reads it per message. `awareness_ids` tracks the awareness
    client ids this socket has declared, so its presence can be removed on
    disconnect."""
    user_id: int
    can_write: bool
    awareness_ids: set[int] = field(default_factory=set)


class Room:
    def __init__(self, key: RoomKey) -> None:
        self.key = key
        self.doc = Doc()
        # Per-room presence. set_local_state(None) clears the server doc's own
        # awareness entry (a fresh Awareness seeds states[doc.client_id] = {}),
        # so the server never broadcasts a phantom cursor.
        self.awareness = P.Awareness(self.doc)
        self.awareness.set_local_state(None)
        # client_id → user_id ownership bindings (Slice 3b-5d.D2). First-announce,
        # per-USER, ROOM-lifetime (the map dies with the Room on eviction): a
        # socket may relay awareness entries only for ids bound to its own
        # authenticated user. Room-lifetime (not socket-lifetime) is what blocks
        # ownership inversion — a griefer cannot claim a blipped peer's id, while
        # the victim's same-user reconnect re-claims it freely (R2-2).
        self._awareness_bindings: dict[int, int] = {}
        self.clients: dict = {}
        self.ops = 0            # in-flight server ops (kernel accepts) — Codex H1
        self.poisoned = False
        self.evicted = False   # tombstone: in-flight ops recheck & abort (Slice 3b-1)
        self._lock = asyncio.Lock()
        self._pending_on_evict = None   # deferred last-leave checkpoint (Slice 3a c)
        # Serialize permission reconciliation per-room (Slice 3a cluster a/b).
        # Lock-acquisition ORDER (deadlock-freedom):
        #   recon_lock → manager._lock (via MANAGER.release/drop_client) → room._lock
        #   (via remove_client_presence, run AFTER manager._lock is released).
        # No path acquires recon_lock while holding manager._lock or room._lock, and
        # no path acquires recon_lock while holding room._lock — so the chain above is
        # acyclic → deadlock-free. Note (Slice 3b-4): refresh_permissions may now
        # TRANSITIVELY take room._lock, because release/drop_client call
        # remove_client_presence (presence removal for surviving peers) — but always
        # AFTER manager._lock has been released, so no room._lock-under-manager._lock
        # edge exists. The WS route still takes recon_lock for the self-reconcile,
        # releases it, THEN takes room._lock for the initial message.
        # Note: reconcile_client may run checkpoint on_evict (a threadpool git commit
        # + advisory commit-mutex wait) while holding recon_lock, briefly serializing
        # connects to the same room — acceptable (revokes are rare; connects to OTHER
        # rooms are unaffected).
        self._recon_lock = asyncio.Lock()

    def initial_message(self) -> bytes:
        """The SYNC_STEP1 the server sends a client on connect."""
        return P.create_sync_message(self.doc)

    async def awareness_snapshot(self) -> bytes | None:
        """The current presence as an AWARENESS wire frame, to prime a joining
        client. None if no presence yet. Excludes the server's own id (cleared
        at init)."""
        async with self._lock:
            ids = list(self.awareness.states)
            if not ids:
                return None
            return P.create_awareness_message(self.awareness.encode_awareness_update(ids))

    async def handle(
        self,
        *,
        sender,
        state: ClientState,
        message: bytes,
        persist: PersistFn,
    ) -> None:
        if P.is_sync_step1(message):
            # Read path: always answer with our state (a SYNC_STEP2 reply). A
            # malformed STEP1 body (truncated/garbage state vector) must be
            # dropped, not raised — symmetric with the write-path guard below,
            # and reachable by any connected member (the read path is ungated).
            # Lock the doc read so a concurrent kernel accept (worker thread, under
            # this same lock) cannot mutate room.doc while we read it (Codex H2).
            async with self._lock:
                try:
                    reply = P.handle_sync_message(message[1:], self.doc)
                except Exception:
                    logger.warning(
                        "collab.malformed_step1",
                        extra={"event": "collab_malformed_step1", "room": str(self.key)},
                    )
                    return
            if reply is not None:
                await sender.send_bytes(reply)
            return

        if P.is_sync_write(message):
            if not state.can_write:
                return  # cheap early reject for a known non-writer
            try:
                update = P.extract_update(message)
            except Exception:
                logger.warning(
                    "collab.malformed_frame",
                    extra={"event": "collab_malformed_frame", "room": str(self.key)},
                )
                return  # drop malformed frame: not decoded, not persisted, not broadcast
            if not update or update == _EMPTY_UPDATE:
                return
            async with self._lock:
                # LIVE re-check under the lock: a concurrent revoke/downgrade
                # (Slice-2c refresh_permissions, same event loop) may have flipped
                # state.can_write AFTER the early check while this frame waited on
                # the lock. There is NO await between this check and apply_update,
                # so check→apply is atomic. (Codex High #1)
                if not state.can_write:
                    return
                if self.evicted:
                    return  # room tombstoned mid-flight — drop (no apply/persist/broadcast)
                # Apply FIRST so we never persist an update that won't replay
                # (keeps the durable log clean by construction). apply_update is
                # atomic: a malformed update raises without mutating the doc.
                try:
                    self.doc.apply_update(update)
                except Exception:
                    logger.warning(
                        "collab.malformed_update",
                        extra={"event": "collab_malformed_update", "room": str(self.key)},
                    )
                    return  # drop malformed: not persisted, not broadcast
                # Then persist DURABLY. If this fails the doc is ahead of the
                # log: poison the room so it is evicted and rebuilt from the log.
                try:
                    await persist(state.user_id, update)
                except Exception as e:
                    self.poisoned = True
                    raise PersistError(str(self.key)) from e
            # Broadcast outside the lock (CRDT tolerates reordering; a slow
            # client must not block other writers).
            if self.evicted:
                return  # evicted after persist — skip broadcast (sockets already closed)
            await self._broadcast(P.create_update_message(update), exclude=sender)
            return

        if P.is_awareness(message):
            # Presence relay: ungated (not a doc mutation) and NEVER persisted.
            # Parse OUTSIDE the lock (bounded, O(payload) — f535f6c hardening).
            try:
                payload = P.extract_awareness_update(message)
                entries = P.read_awareness_entries(payload)
            except Exception:
                logger.warning(
                    "collab.malformed_awareness",
                    extra={"event": "collab_malformed_awareness", "room": str(self.key)},
                )
                return
            async with self._lock:
                # Ownership filter (Slice 3b-5d.D2, closes the 3b-5b deferral): a
                # socket may introduce, update, or remove awareness entries ONLY
                # for client-ids bound to its own authenticated user. This kills
                # the ACTIVE relay-spoof (a high-clock hide/fake of a peer's
                # caret is clock-gated on receivers and does NOT self-heal)
                # before anyone — the victim included — receives it.
                kept: list[P.AwarenessEntry] = []
                foreign: list[P.AwarenessEntry] = []
                for e in entries:
                    owner = self._awareness_bindings.get(e.client_id)
                    (kept if owner in (None, state.user_id) else foreign).append(e)
                if foreign:
                    # Foreign NULL-state entries are ROUTINE: y-protocols' 3s
                    # local timeout-scrub of a backgrounded tab ships with
                    # origin 'timeout', which the provider's origin guard does
                    # not suppress → INFO. Foreign NON-null entries are the true
                    # spoof signature → WARNING. The level split is load-bearing:
                    # without it the daily scrub noise drowns the spoof signal.
                    stale = [e.client_id for e in foreign if e.is_null]
                    spoof = [e.client_id for e in foreign if not e.is_null]
                    if stale:
                        logger.info(
                            "collab.awareness_stale_scrub_filtered",
                            extra={"event": "collab_awareness_stale_scrub_filtered",
                                   "room": str(self.key), "user_id": state.user_id,
                                   "client_ids": stale},
                        )
                    if spoof:
                        logger.warning(
                            "collab.awareness_foreign_filtered",
                            extra={"event": "collab_awareness_foreign_filtered",
                                   "room": str(self.key), "user_id": state.user_id,
                                   "client_ids": spoof},
                        )
                if entries and not kept:
                    return  # pure-foreign frame (routine stale-scrub): drop
                kept_ids = {e.client_id for e in kept}
                new_ids = kept_ids - self._awareness_bindings.keys()
                if new_ids:
                    # Per-user binding quota — see _MAX_AWARENESS_BINDINGS_PER_USER.
                    # Over-cap: drop ONLY the would-be-new-binding entries. Bound
                    # renewals (heartbeats/caret moves) always pass, and foreign
                    # entries were already logged above — a mixed quota+spoof
                    # frame still fires the spoof signal (Codex Medium).
                    bound = sum(
                        1 for uid in self._awareness_bindings.values()
                        if uid == state.user_id
                    )
                    if bound + len(new_ids) > _MAX_AWARENESS_BINDINGS_PER_USER:
                        logger.warning(
                            "collab.awareness_bindings_capped",
                            extra={"event": "collab_awareness_bindings_capped",
                                   "room": str(self.key), "user_id": state.user_id,
                                   "bound": bound, "new": len(new_ids)},
                        )
                        kept = [
                            e for e in kept if e.client_id in self._awareness_bindings
                        ]
                        kept_ids = {e.client_id for e in kept}
                        new_ids = set()
                        if not kept:
                            return
                relay = P.filter_awareness_update(payload, entries, kept)
                if relay is None:  # unreachable (kept is non-empty) — defensive
                    return
                try:
                    # Apply the FILTERED payload, so a spoofed entry can never
                    # poison the snapshot new joiners are primed with.
                    self.awareness.apply_awareness_update(relay, origin="client")
                except Exception:
                    logger.warning(
                        "collab.malformed_awareness_update",
                        extra={"event": "collab_malformed_awareness_update",
                               "room": str(self.key)},
                    )
                    return  # nothing bound, nothing relayed (bind-after-accept)
                # Bind AFTER a successful apply, and only the new ids pycrdt
                # actually ACCEPTED into states (Codex High): a parse-valid
                # entry that fails or is clock-ignored on apply must not reserve
                # a binding, or a stealth pre-claim (malformed JSON state /
                # stale clock — no visible caret either way) would filter the
                # id's real owner for the room's life.
                for cid in new_ids:
                    if cid in self.awareness.states:
                        self._awareness_bindings[cid] = state.user_id
                        logger.info(
                            "collab.awareness_bind",
                            extra={"event": "collab_awareness_bind",
                                   "room": str(self.key),
                                   "client_id": cid, "user_id": state.user_id},
                        )
                # Attribute to THIS socket only ids bound to THIS user (drives
                # disconnect removal). The 3b-5b owned_by_other_users guard is
                # subsumed: a foreign-bound id is filtered above, and an
                # unbound-never-accepted id (e.g. a null pre-claim) is never
                # attributed — so this socket's disconnect can never scrub an
                # id someone else binds later. Attribution from kept ids (not a
                # states-diff) keeps R2-2 same-clientID reconnects working.
                state.awareness_ids |= {
                    cid for cid in kept_ids
                    if self._awareness_bindings.get(cid) == state.user_id
                }
            # Relay the FILTERED payload outside the lock (standard y-awareness
            # relay; a slow client must not block the room).
            await self._broadcast(P.create_awareness_message(relay), exclude=sender)
            return

        # Anything else: ignored.
        return

    # --- public accessors for the kernel (Step 1c) ---

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    @property
    def recon_lock(self) -> asyncio.Lock:
        return self._recon_lock

    async def remove_client_presence(self, state: ClientState) -> None:
        """Remove a departed client's awareness ids and broadcast the removal to
        remaining clients so their carets drop immediately (not after the ~30s
        timeout). No-op if the client owned no presence. Callers invoke this only
        when the room still has peers (see RoomManager.release/drop_client)."""
        # ACCEPTED LIMITATION (reconnect clock-collision, Slice 3b-5b #3): removing a
        # REMOTE id here encodes the removal as state=null at the id's CURRENT clock
        # (y-awareness bumps the clock only when a client removes its OWN id). If the
        # same client reconnects with the same doc.clientID, its on-open flush
        # re-announces at that same clock and applyAwarenessUpdate ignores it (a
        # non-null state cannot override an equal-clock entry), so the reconnected
        # caret is briefly invisible to peers. It self-heals at the next awareness
        # renewal: immediately on cursor activity, or <= ~15s via the y-protocols
        # renew interval (outdatedTimeout/2). Inherent to y-awareness + server-side
        # removal; standard y-websocket shares it. Documented, not fixed.
        ids = list(state.awareness_ids)
        if not ids:
            return
        async with self._lock:
            self.awareness.remove_awareness_states(ids, origin="server")
            frame = P.create_awareness_message(self.awareness.encode_awareness_update(ids))
        await self._broadcast(frame, exclude=None)

    async def broadcast(self, frame: bytes) -> None:
        await self._broadcast(frame, exclude=None)

    async def _broadcast(self, frame: bytes, *, exclude) -> None:
        """Send to every client except `exclude`, isolating per-client failures:
        a broken/closed socket is dropped and the rest still receive the frame.
        A dropped client's presence is ALSO scrubbed from the room awareness so a
        hard socket failure cannot leak its cursor into new-joiner snapshots (the
        later release()/drop_client() can no longer recover its ClientState)."""
        dead = []
        for client in list(self.clients):
            if client is exclude:
                continue
            try:
                await client.send_bytes(frame)
            except Exception:
                dead.append(client)
        for client in dead:
            state = self.clients.pop(client, None)
            if state is not None and state.awareness_ids:
                # Synchronous (no await) → atomic; no room lock needed here.
                self.awareness.remove_awareness_states(list(state.awareness_ids), origin="server")


class RoomManager:
    def __init__(self) -> None:
        self._rooms: dict[RoomKey, Room] = {}
        self._lock = asyncio.Lock()
        # Per-key creation locks so cold-start hydration is serialized only
        # per room, never process-wide. Retained after use (bounded by the
        # number of distinct rooms — tiny for this app); not popped, because a
        # concurrent acquirer may still hold a reference.
        self._keylocks: dict[RoomKey, asyncio.Lock] = {}

    def has_room(self, key: RoomKey) -> bool:
        return key in self._rooms

    async def acquire(self, key: RoomKey, *, load: LoadFn, client, state: ClientState) -> Room:
        """Get-or-create the room, replaying persisted updates on cold start,
        and register `client` with its per-connection `state`.

        The cold-start replay (`await load()`) runs OUTSIDE the manager-global
        lock, serialized only by a per-key lock — so hydrating one room never
        blocks connections to unrelated rooms (no process-wide head-of-line
        blocking as the update log grows).
        """
        async with self._lock:
            room = self._rooms.get(key)
            if room is not None:
                room.clients[client] = state
                return room
            keylock = self._keylocks.setdefault(key, asyncio.Lock())

        async with keylock:
            # Re-check under the per-key lock: another creator may have finished
            # while we waited. Register the client in the SAME lock section as the
            # re-fetch so a concurrent operate()-exit cannot evict the room in the
            # gap between fetch and client-add (Codex H1 — the ops-refcount eviction
            # now makes this window reachable; keep register atomic with the fetch).
            async with self._lock:
                room = self._rooms.get(key)
                if room is not None:
                    room.clients[client] = state
            if room is None:
                room = Room(key)
                for update in await load():  # runs OUTSIDE the manager lock
                    room.doc.apply_update(update)
                async with self._lock:
                    self._rooms[key] = room
                    room.clients[client] = state
            return room

    @asynccontextmanager
    async def operate(self, key: RoomKey, *, load: LoadFn):
        """Get-or-create a room for a SERVER operation (no client registered).
        Tracks an in-flight server-op count so eviction is deferred until BOTH
        no clients AND no other server op remain (Codex H1). Does not create its
        OWN checkpoint on evict, but runs any checkpoint `release` deferred
        because this op held `ops > 0` (Slice 3a c)."""
        async with self._lock:
            room = self._rooms.get(key)
            if room is not None:
                room.ops += 1
            else:
                keylock = self._keylocks.setdefault(key, asyncio.Lock())
        if room is None:
            async with keylock:
                # Re-fetch AND bump ops in ONE lock section: a concurrent
                # operate()-exit must not be able to pop the room between our
                # re-fetch and our ops bump, or it would defeat the very H1
                # invariant this refcount adds (never evict while an op is live)
                # and hand back a zombie room detached from the registry.
                async with self._lock:
                    room = self._rooms.get(key)
                    if room is not None:
                        room.ops += 1
                if room is None:
                    room = Room(key)
                    for update in await load():   # OUTSIDE the manager lock
                        room.doc.apply_update(update)
                    async with self._lock:
                        self._rooms[key] = room
                        room.ops += 1
        try:
            yield room
        finally:
            pending = None
            async with self._lock:
                room.ops -= 1
                if not room.clients and room.ops == 0 and self._rooms.get(key) is room:
                    self._rooms.pop(key, None)
                    pending = room._pending_on_evict
                    room._pending_on_evict = None
            if pending is not None:
                await pending(room)   # OUTSIDE the manager lock (mirrors release)

    def get(self, key: RoomKey) -> Room | None:
        return self._rooms.get(key)

    async def release(self, key: RoomKey, *, client, on_evict=None) -> None:
        """Deregister a client; evict the room when the last one leaves. If a
        room is evicted and `on_evict` is given, call it (e.g. to checkpoint)
        OUTSIDE the manager lock — never hold the global lock across I/O.

        If the room is client-empty but a server op is still in flight (ops>0),
        the eviction (and checkpoint) are deferred: `on_evict` is stashed on the
        room and run by `operate()`-exit when it evicts (Slice 3a c)."""
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
                    # Client-empty but an op is in flight: defer the checkpoint to
                    # operate()-exit so a straddling op cannot swallow it (Slice 3a c).
                    room._pending_on_evict = on_evict
        # Presence removal + checkpoint both run OUTSIDE the manager lock.
        if survivor is not None and removed_state is not None:
            await survivor.remove_client_presence(removed_state)
        if evicted is not None and on_evict is not None:
            await on_evict(evicted)

    async def drop_client(self, key: RoomKey, client) -> None:
        """Deregister a client WITHOUT checkpointing, evicting the room if it
        becomes empty. Used by permission revocation and teardown where a
        last-leave checkpoint is wrong (the workspace may be soft-deleted / the
        member just lost access). release() is the checkpointing counterpart."""
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

    async def evict(self, key: RoomKey) -> None:
        """Force-remove a room regardless of connected clients (poison recovery),
        and CLOSE every client socket so no client keeps editing an orphaned
        in-memory doc — the next connection rebuilds cleanly from the durable log.
        (Closes the Slice-1 residual where evict left other clients connected.)"""
        async with self._lock:
            room = self._rooms.pop(key, None)
        if room is None:
            return
        room.evicted = True   # tombstone: block any in-flight op's persist/broadcast (Slice 3b-1)
        for client in list(room.clients):
            with suppress(Exception):
                await client.close(code=_WS_INTERNAL_ERROR)


# Module-global registry (single-instance backend).
MANAGER = RoomManager()
