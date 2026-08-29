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
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field

from pycrdt import Doc

from call_manager.proposal_assist.drafting.contract import WorkspaceRetired
from call_manager.proposal_assist.lifecycle import LIFECYCLE, WorkspaceLifecycle

from . import protocol as P

RoomKey = tuple[uuid.UUID, str]
PersistFn = Callable[[int, bytes], Awaitable[None]]
LoadFn = Callable[[], Awaitable[list[bytes]]]


@dataclass(frozen=True)
class BranchSwitchTeardown:
    """What a branch-switch teardown achieved. `locked` False means at least one
    room was tombstoned WITHOUT room.lock, so a client persist may still be in
    flight — the switch must abort. The teardown itself always completed."""

    closed: int
    locked: bool


# An empty Yjs update is two zero bytes; nothing to apply/persist/broadcast.
_EMPTY_UPDATE = b"\x00\x00"

# RFC 6455 close code: server error (used for poison-room eviction)
_WS_INTERNAL_ERROR = 1011

# RFC 6455 close code: policy violation. Retirement closes editors with THIS, not
# 1011 (server error): a soft-deleted workspace is a policy decision, not a crash.
_WS_POLICY_VIOLATION = 1008

# RFC 6455 close code: try again later. A branch switch closes editors with THIS
# (not 1008) because the workspace still exists on another branch — the client
# must reconnect immediately and will receive a fresh snapshot. 1008 is terminal
# on the client (frontend/src/collab/collabProvider.ts:234).
_WS_TRY_AGAIN_LATER = 1013

# Bound on how long retire_workspace waits for an in-flight server op (a kernel
# accept mid-write) to leave before it declares a room's retirement done. On
# overrun it logs and proceeds: B4's under-mutex re-read of soft_deleted_at is the
# durable backstop for those ops (a straggler refuses to commit anyway). Applied
# PER room, so both artifacts drain within ~2*this; well inside the 5s barrier.
_RETIRE_OPS_DRAIN_SECONDS = 2.0

# Bound on how long retire_workspace waits for room.lock in order to flip the
# `evicted` tombstone. Room.handle holds that lock across its persist await, so
# in practice this is one DB round-trip. On overrun we set the flag
# unsynchronized (which still stops every LATER frame) and log.
_RETIRE_LOCK_WAIT_SECONDS = 1.0

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
        self.ops = 0  # in-flight server ops (kernel accepts) — Codex H1
        self.poisoned = False
        self.evicted = False  # tombstone: in-flight ops recheck & abort (Slice 3b-1)
        self._lock = asyncio.Lock()
        self._pending_on_evict = None  # deferred last-leave checkpoint (Slice 3a c)
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
            await self._handle_awareness(
                sender=sender, state=state, payload=payload, entries=entries
            )
            return

        # Anything else: ignored.
        return

    async def _handle_awareness(
        self,
        *,
        sender,
        state: ClientState,
        payload: bytes,
        entries: list[P.AwarenessEntry],
    ) -> None:
        """Awareness relay policy (Slice 3b-5d.D2, closes the 3b-5b deferral).

        Ownership filter: a socket may introduce, update, or remove awareness
        entries ONLY for client-ids bound to its own authenticated user. This
        kills the ACTIVE relay-spoof (a high-clock hide/fake of a peer's caret
        is clock-gated on receivers and does NOT self-heal) before anyone —
        the victim included — receives it. Stages, all under the room lock:
        partition → log split → quota/recycle → pre-validate → apply filtered →
        bind-after-accept → attribute; the relay itself runs OUTSIDE the lock.
        Every early return drops the frame (nothing applied, nothing relayed).
        """
        async with self._lock:
            kept: list[P.AwarenessEntry] = []
            filtered: list[P.AwarenessEntry] = []
            for e in entries:
                owner = self._awareness_bindings.get(e.client_id)
                # Own-bound: anything goes (announce, renewal, self-removal).
                # Unbound: only NON-null may pass (a first announce, which
                # bind-after-accept will bind). An unbound NULL is filtered:
                # pycrdt records an unknown-id removal's clock in its private
                # _meta map without entering states, so applying/relaying a
                # null@huge-clock for a not-yet-bound id would permanently
                # clock-gate the id's real owner (a silent hide) and grow
                # _meta without bound — nulls never bind, so the binding
                # quota can't see them (review #2/#3).
                if owner == state.user_id or (owner is None and not e.is_null):
                    kept.append(e)
                else:
                    filtered.append(e)
            if filtered:
                # NULL-state entries are ROUTINE: y-protocols' 3s local
                # timeout-scrub of a backgrounded tab ships with origin
                # 'timeout', which the provider's origin guard does not
                # suppress → INFO (each peer scrubs locally; the relay is
                # redundant). Foreign-bound NON-null entries are the true
                # spoof signature → WARNING. The level split is load-bearing:
                # without it the daily scrub noise drowns the spoof signal.
                stale = [e.client_id for e in filtered if e.is_null]
                spoof = [e.client_id for e in filtered if not e.is_null]
                if stale:
                    logger.info(
                        "collab.awareness_stale_scrub_filtered",
                        extra={
                            "event": "collab_awareness_stale_scrub_filtered",
                            "room": str(self.key),
                            "user_id": state.user_id,
                            "client_ids": stale,
                        },
                    )
                if spoof:
                    logger.warning(
                        "collab.awareness_foreign_filtered",
                        extra={
                            "event": "collab_awareness_foreign_filtered",
                            "room": str(self.key),
                            "user_id": state.user_id,
                            "client_ids": spoof,
                        },
                    )
            if not kept:
                return  # pure-filtered frame (routine stale-scrub) or empty frame
            kept_ids = {e.client_id for e in kept}
            new_ids = kept_ids - self._awareness_bindings.keys()
            if new_ids:
                # Per-user binding quota — see _MAX_AWARENESS_BINDINGS_PER_USER.
                mine = [
                    cid for cid, uid in self._awareness_bindings.items() if uid == state.user_id
                ]
                overflow = len(mine) + len(new_ids) - _MAX_AWARENESS_BINDINGS_PER_USER
                if overflow > 0:
                    # Recycle the user's OLDEST dead bindings first (id no
                    # longer in states — a stale reload identity, not a live
                    # caret): a long-lived room + many reloads must not
                    # silence a legit user (review #4). Live-caret bindings
                    # are never recycled; dict order = oldest first. A
                    # recycled id becomes claimable again — same class as
                    # the documented binding-reset-race residual, and any
                    # reclaim is bind-logged.
                    dead = [c for c in mine if c not in self.awareness.states]
                    for cid in dead[:overflow]:
                        del self._awareness_bindings[cid]
                    overflow -= len(dead[:overflow])
                if overflow > 0:
                    # All bound ids are live carets (the abuse shape): drop
                    # ONLY the would-be-new-binding entries. Bound renewals
                    # (heartbeats/caret moves) always pass, and filtered
                    # entries were already logged above — a mixed
                    # quota+spoof frame still fires the spoof signal.
                    logger.warning(
                        "collab.awareness_bindings_capped",
                        extra={
                            "event": "collab_awareness_bindings_capped",
                            "room": str(self.key),
                            "user_id": state.user_id,
                            "bound": len(mine),
                            "new": len(new_ids),
                        },
                    )
                    kept = [e for e in kept if e.client_id not in new_ids]
                    kept_ids = {e.client_id for e in kept}
                    new_ids = set()
                    if not kept:
                        return
            if len(kept) > 1:
                # pycrdt applies entry-by-entry (json.loads inline), so a
                # bad tail after a good head would leave the head applied —
                # in the snapshot, unbound and unattributed, unscrubable
                # (review #1). Pre-validate every state so apply stays
                # all-or-nothing. Single-entry frames (the ~universal legit
                # shape) skip this: a lone failing entry mutates nothing.
                for e in kept:
                    if not e.is_null:
                        try:
                            json.loads(payload[e.state_start : e.end])
                        except ValueError:
                            logger.warning(
                                "collab.malformed_awareness_update",
                                extra={
                                    "event": "collab_malformed_awareness_update",
                                    "room": str(self.key),
                                },
                            )
                            return
            # kept is non-empty here, so the filter never returns None.
            relay = P.filter_awareness_update(payload, entries, kept)
            try:
                # Apply the FILTERED payload, so a spoofed entry can never
                # poison the snapshot new joiners are primed with.
                self.awareness.apply_awareness_update(relay, origin="client")
            except Exception:
                logger.warning(
                    "collab.malformed_awareness_update",
                    extra={"event": "collab_malformed_awareness_update", "room": str(self.key)},
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
                        extra={
                            "event": "collab_awareness_bind",
                            "room": str(self.key),
                            "client_id": cid,
                            "user_id": state.user_id,
                        },
                    )
            # Attribute to THIS socket only ids bound to THIS user (drives
            # disconnect removal). The 3b-5b owned_by_other_users guard is
            # subsumed: a foreign-bound id is filtered above, and an
            # unbound-never-accepted id (e.g. a null pre-claim) is never
            # attributed — so this socket's disconnect can never scrub an
            # id someone else binds later. Attribution from kept ids (not a
            # states-diff) keeps R2-2 same-clientID reconnects working.
            state.awareness_ids |= {
                cid for cid in kept_ids if self._awareness_bindings.get(cid) == state.user_id
            }
        # Relay the FILTERED payload outside the lock (standard y-awareness
        # relay; a slow client must not block the room).
        await self._broadcast(P.create_awareness_message(relay), exclude=sender)

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
    def __init__(self, lifecycle: WorkspaceLifecycle | None = None) -> None:
        self._rooms: dict[RoomKey, Room] = {}
        self._lock = asyncio.Lock()
        # Per-key creation locks so cold-start hydration is serialized only
        # per room, never process-wide. Retained after use (bounded by the
        # number of distinct rooms — tiny for this app); not popped, because a
        # concurrent acquirer may still hold a reference.
        self._keylocks: dict[RoomKey, asyncio.Lock] = {}
        # Single authority for workspace retirement (Slice C0). Guards call
        # self._lifecycle.is_retired() instead of a local set, so a restore
        # written to the authority is visible here without a separate clear.
        self._lifecycle = lifecycle if lifecycle is not None else LIFECYCLE

    def has_room(self, key: RoomKey) -> bool:
        return key in self._rooms

    async def acquire(self, key: RoomKey, *, load: LoadFn, client, state: ClientState) -> Room:
        """Get-or-create the room, replaying persisted updates on cold start,
        and register `client` with its per-connection `state`.

        The cold-start replay (`await load()`) runs OUTSIDE the manager-global
        lock, serialized only by a per-key lock — so hydrating one room never
        blocks connections to unrelated rooms (no process-wide head-of-line
        blocking as the update log grows).

        A retired (soft-deleted) workspace never gets a room: rejected on entry
        and RE-CHECKED after the out-of-lock ``await load()``, because retirement
        can land inside that window.
        """
        workspace_id = key[0]
        async with self._lock:
            # is_retired is lock-free and that is load-bearing — this can run
            # inside a hook the coordinator invokes while holding the lifecycle lock
            # (so it must NOT take that lock).
            if self._lifecycle.is_retired(workspace_id):
                raise WorkspaceRetired(workspace_id)
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
                if self._lifecycle.is_retired(workspace_id):
                    raise WorkspaceRetired(workspace_id)
                room = self._rooms.get(key)
                if room is not None:
                    room.clients[client] = state
            if room is None:
                room = Room(key)
                for update in await load():  # runs OUTSIDE the manager lock
                    room.doc.apply_update(update)
                async with self._lock:
                    # Re-check AFTER the out-of-lock load(): retirement may have
                    # landed while we hydrated. Publishing now would resurrect a
                    # room for a deleted workspace that retirement already swept.
                    if self._lifecycle.is_retired(workspace_id):
                        raise WorkspaceRetired(workspace_id)
                    self._rooms[key] = room
                    room.clients[client] = state
            return room

    @asynccontextmanager
    async def operate(self, key: RoomKey, *, load: LoadFn):
        """Get-or-create a room for a SERVER operation (no client registered).
        Tracks an in-flight server-op count so eviction is deferred until BOTH
        no clients AND no other server op remain (Codex H1). Does not create its
        OWN checkpoint on evict, but runs any checkpoint `release` deferred
        because this op held `ops > 0` (Slice 3a c).

        A retired (soft-deleted) workspace is rejected on entry and RE-CHECKED
        after the out-of-lock `await load()` — the straddle kernel_accept.py:71
        rides."""
        workspace_id = key[0]
        async with self._lock:
            # is_retired is lock-free and that is load-bearing — this can run
            # inside a hook the coordinator invokes while holding the lifecycle lock
            # (so it must NOT take that lock).
            if self._lifecycle.is_retired(workspace_id):
                raise WorkspaceRetired(workspace_id)
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
                    if self._lifecycle.is_retired(workspace_id):
                        raise WorkspaceRetired(workspace_id)
                    room = self._rooms.get(key)
                    if room is not None:
                        room.ops += 1
                if room is None:
                    room = Room(key)
                    for update in await load():  # OUTSIDE the manager lock
                        room.doc.apply_update(update)
                    async with self._lock:
                        # Re-check AFTER load(): retirement may have landed while
                        # we hydrated. Publishing now would resurrect a room that
                        # retirement already swept, and this op would then write
                        # to a deleted workspace.
                        if self._lifecycle.is_retired(workspace_id):
                            raise WorkspaceRetired(workspace_id)
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
                await pending(room)  # OUTSIDE the manager lock (mirrors release)

    def get(self, key: RoomKey) -> Room | None:
        return self._rooms.get(key)

    async def snapshot_rooms(self) -> list[Room]:
        """A point-in-time list of live rooms, taken under the manager lock and
        returned so callers can iterate WITHOUT holding it across per-room awaits
        (preserves the recon_lock -> manager._lock -> room._lock order)."""
        async with self._lock:
            return list(self._rooms.values())

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
                    survivor = room  # peers remain → drop the leaver's caret
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
        room.evicted = True  # tombstone: block any in-flight op's persist/broadcast (Slice 3b-1)
        for client in list(room.clients):
            with suppress(Exception):
                await client.close(code=_WS_INTERNAL_ERROR)

    async def retire_workspace(self, workspace_id: uuid.UUID, generation: int) -> int:
        """Collab retirement hook (coordinator-invoked). Tears down BOTH artifact rooms:
        pop each, tombstone it (room.evicted, flipped UNDER room.lock so it linearizes
        against in-flight client persists), close every editor with 1008, and drain
        any in-flight server op. NEVER checkpoints — the workspace is soft-deleted,
        so a last-leave checkpoint would write to a dead workspace (and B4 would
        refuse it under the mutex anyway); this is drop_client semantics for the
        whole room. Idempotent. Returns the number of sockets closed (tests/logging;
        the coordinator discards it).

        The workspace retirement transition is applied by the coordinator BEFORE this
        hook is invoked — the coordinator holds the lifecycle lock across the whole
        fan-out, so the tombstone is set when this runs. This hook must NOT tombstone
        again (that would deadlock on the lock its caller holds); it is ONLY teardown.

        *generation* is unused here: the coordinator gates the whole fan-out on
        this generation while holding the lifecycle lock, so this hook is never
        invoked for a superseded transition and must not re-check (and could not:
        taking the lock here deadlocks).

        The two artifact rooms are torn down CONCURRENTLY. They share nothing but
        self._lock (held only across the pop), so serialising them merely doubled
        the hook's worst case — 2 x (_RETIRE_LOCK_WAIT_SECONDS + _RETIRE_OPS_DRAIN_
        SECONDS) = 6.0s — past the retirement barrier the sync delete route blocks
        on. Concurrent, the worst case is one room's 3.0s. See
        retirement._RETIREMENT_BARRIER_SECONDS, whose budget this feeds."""
        # return_exceptions=True keeps "tear down BOTH rooms" a guarantee rather
        # than an accident: a bare gather would propagate the first failure while
        # the sibling task ran on detached, and a second failure would then surface
        # as an unretrieved-task-exception warning. Nothing in _retire_room is
        # expected to raise (the wait_for timeout is caught, the closes are
        # suppressed), so this is a floor, not a routine path.
        results = await asyncio.gather(
            *(self._retire_room(workspace_id, artifact) for artifact in ("draft", "spec")),
            return_exceptions=True,
        )
        closed = 0
        for artifact, result in zip(("draft", "spec"), results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    "collab.retire_room_failed",
                    extra={
                        "event": "collab_retire_room_failed",
                        "room": str((workspace_id, artifact)),
                    },
                    exc_info=result,
                )
                continue
            n, _got_lock = result
            closed += n
        return closed

    async def prepare_branch_switch(self, workspace_id: uuid.UUID) -> BranchSwitchTeardown:
        """Tear both artifact rooms down for a branch switch (spec §4.2 step 4).

        Differs from `retire_workspace` in exactly two ways, both deliberate:
        it closes 1013 rather than 1008, and it REPORTS whether it linearized
        against in-flight client persists instead of swallowing the timeout.

        It does NOT abort on a lock timeout. The teardown pops each room before
        it reaches for `room.lock`, so returning early would leave a room out of
        the registry, un-tombstoned, and still writable through the reference its
        clients already hold — a retry could not find it, and its next keystroke
        would land after that retry truncated the log. Abort the SWITCH, never
        the teardown: this finishes, and the caller acts on `locked`.

        Never checkpoints. The switch checkpoints separately, from the frozen
        LOG, after this returns.
        """
        results = await asyncio.gather(
            *(
                self._retire_room(workspace_id, artifact, close_code=_WS_TRY_AGAIN_LATER)
                for artifact in ("draft", "spec")
            ),
            return_exceptions=True,
        )
        closed = 0
        locked = True
        for artifact, result in zip(("draft", "spec"), results, strict=True):
            if isinstance(result, BaseException):
                # A teardown that RAISED still finished its close-and-tombstone
                # (that work is in a `finally`), so no orphan survives — but it
                # cannot say whether it linearized. Treat it as NOT linearized so
                # the switch aborts. `return_exceptions=True` is what keeps "tear
                # BOTH rooms down" a guarantee rather than an accident: a bare
                # gather would abandon the sibling task on the first failure.
                locked = False
                logger.warning(
                    "collab.branch_switch_teardown_failed",
                    extra={
                        "event": "collab_branch_switch_teardown_failed",
                        "room": str((workspace_id, artifact)),
                    },
                    exc_info=result,
                )
                continue
            n, got_lock = result
            closed += n
            locked = locked and got_lock
        return BranchSwitchTeardown(closed=closed, locked=locked)

    async def _retire_room(
        self, workspace_id: uuid.UUID, artifact: str, *, close_code: int = _WS_POLICY_VIOLATION
    ) -> tuple[int, bool]:
        """Tear down ONE artifact room. Returns (sockets closed, got_room_lock).

        `close_code` is a protocol decision the CALLER owns, because the same
        teardown serves two situations the client must distinguish:
        retirement closes 1008 (terminal — the workspace is gone, retrying is
        pointless) and a branch switch closes 1013 (come straight back — the
        artifact still exists on another branch). 1008 is terminal on the client
        (frontend/src/collab/collabProvider.ts:234).

        `got_room_lock` is False when the tombstone had to be set without
        `room.lock`. Retirement ignores it — the straggler's commit lands in a
        log nobody will read. A branch switch MUST NOT ignore it: that commit
        would ride into the target branch. Either way this function finishes the
        teardown; it never returns leaving a popped room live.
        """
        loop = asyncio.get_running_loop()
        closed = 0
        key = (workspace_id, artifact)
        # Pop under the manager lock so no new op/client can attach after this.
        async with self._lock:
            room = self._rooms.pop(key, None)
        if room is None:
            return 0, True  # nothing to tear down IS a linearized teardown
        # Tombstone UNDER room.lock — this is the client-write linearization
        # point, not a cosmetic flag set. Room.handle's write path checks
        # `self.evicted` and then awaits persist(...) INSIDE this same lock,
        # and that persist commits a YjsUpdate row with NO commit mutex and NO
        # soft_deleted_at read, so it is invisible to both room.ops and B4.
        # Taking the lock therefore buys two things at once:
        #   (a) when we get it, no client persist is in flight;
        #   (b) every later frame sees evicted and drops before apply/persist.
        # No inversion risk: we released self._lock at the pop above and hold
        # NOTHING while waiting, and Room.handle never waits on a manager lock.
        # Bounded like the ops drain — a wedged DB write must not eat the whole
        # retirement barrier; on timeout we set the flag anyway and log, and
        # the straggler's own commit remains the documented residual.
        locked = True
        try:
            try:
                await asyncio.wait_for(
                    _set_evicted_under_lock(room), timeout=_RETIRE_LOCK_WAIT_SECONDS
                )
            except TimeoutError:
                locked = False
                room.evicted = True  # unsynchronized fallback; still stops LATER frames
                logger.warning(
                    "collab.retire_lock_wait_timeout",
                    extra={"event": "collab_retire_lock_wait_timeout", "room": str(key)},
                )
        finally:
            # Never leave a popped room live, whatever happened above. The close
            # loop below this try/finally is outside, so an exception or early
            # return from the lock-wait block would otherwise leave clients open.
            # This belt ensures the tombstone is always set; the close loop is the
            # braces that drains the sockets.
            room.evicted = True
        # Flip can_write + close under recon_lock (parity with
        # revoke_user_sockets), NOT room.lock — Room.handle holds room.lock, so
        # taking it here for the close loop risks a lock-order inversion. We
        # popped the whole room, so there are no surviving peers and no presence
        # removal is needed.
        #
        # BOTH the lock wait and the closes are BOUNDED, and neither is allowed to
        # decide whether the teardown completes. Two reasons:
        #
        # 1. LOCK ORDER. This class's own note above records that
        #    `reconcile_client` may run a checkpoint — "a threadpool git commit +
        #    advisory commit-mutex wait" — while holding recon_lock. So the edge
        #    `recon_lock -> commit mutex` exists. A branch switch calls this
        #    teardown while HOLDING the commit mutex, which adds
        #    `commit mutex -> recon_lock` and closes the cycle. Bounding the wait
        #    turns a mutual stall into a bounded, logged degradation.
        # 2. `await client.close()` on a wedged socket is otherwise unbounded, and
        #    the caller's cross-thread `fut.result(timeout=...)` does NOT cancel
        #    this coroutine — it only stops the request thread waiting. An
        #    unbounded close here would keep recon_lock and the client references
        #    alive indefinitely.
        #
        # Safety does not rest on any of this: `room.evicted` is already set
        # UNCONDITIONALLY in the finally above, and that is what stops writes.
        # Closing sockets is best-effort liveness (it makes clients reconnect
        # promptly), and best-effort work must be bounded.
        try:
            await asyncio.wait_for(room.recon_lock.acquire(), timeout=_RETIRE_LOCK_WAIT_SECONDS)
        except TimeoutError:
            logger.warning(
                "collab.retire_recon_lock_timeout",
                extra={"event": "collab_retire_recon_lock_timeout", "room": str(key)},
            )
            # Proceed WITHOUT the lock. The room is popped and tombstoned, so no
            # surviving peer needs presence removal and no write can land.
            closed += await _close_all(room, close_code)
        else:
            try:
                closed += await _close_all(room, close_code)
            finally:
                room.recon_lock.release()
        # Drain any in-flight server op: the room is popped so no NEW op can bump
        # room.ops, but one already inside operate() may still be mid-write. Poll
        # ops to zero under a bounded deadline, then proceed regardless (for THESE
        # ops B4's mutex is the durable backstop; overrun is logged, not fatal).
        deadline = loop.time() + _RETIRE_OPS_DRAIN_SECONDS
        while room.ops > 0 and loop.time() < deadline:
            await asyncio.sleep(0.01)
        if room.ops > 0:
            logger.warning(
                "collab.retire_ops_drain_timeout",
                extra={
                    "event": "collab_retire_ops_drain_timeout",
                    "room": str(key),
                    "ops": room.ops,
                },
            )
        return closed, locked


async def _close_all(room: Room, close_code: int) -> int:
    """Close every client socket CONCURRENTLY under one bounded deadline.

    Sequential closes let one wedged socket hold up all the others (and, on the
    branch-switch path, hold recon_lock while the switch holds the commit mutex).
    Returns how many sockets were asked to close — the count is for logging, and
    a socket whose close timed out is still counted: it has been told, its
    `can_write` is already False, and the room is tombstoned.
    """
    clients = list(room.clients.items())
    for _client, state in clients:
        state.can_write = False

    async def _one(client) -> None:
        with suppress(Exception):
            await client.close(code=close_code)

    if clients:
        with suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*(_one(c) for c, _ in clients), return_exceptions=True),
                timeout=_RETIRE_LOCK_WAIT_SECONDS,
            )
    return len(clients)


async def _set_evicted_under_lock(room: Room) -> None:
    """Flip the room tombstone while holding room.lock, so it linearizes against
    Room.handle's write path (which checks `evicted` and awaits persist under that
    same lock)."""
    async with room.lock:
        room.evicted = True


# Module-global registry (single-instance backend).
MANAGER = RoomManager(lifecycle=LIFECYCLE)
