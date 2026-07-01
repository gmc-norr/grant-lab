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

from pycrdt import Doc

from . import protocol as P

RoomKey = tuple[uuid.UUID, str]
PersistFn = Callable[[int, bytes], Awaitable[None]]
LoadFn = Callable[[], Awaitable[list[bytes]]]

# An empty Yjs update is two zero bytes; nothing to apply/persist/broadcast.
_EMPTY_UPDATE = b"\x00\x00"


logger = logging.getLogger(__name__)


class PersistError(Exception):
    """Durable persistence of an applied update failed.

    The in-memory doc is now AHEAD of the durable log, so the room is poisoned
    and must be evicted and rebuilt from the log (the route does this). The
    sender's CRDT client re-offers its update on reconnect, so no data is lost.
    """


class Room:
    def __init__(self, key: RoomKey) -> None:
        self.key = key
        self.doc = Doc()
        self.clients: set = set()
        self.poisoned = False
        self._lock = asyncio.Lock()

    def initial_message(self) -> bytes:
        """The SYNC_STEP1 the server sends a client on connect."""
        return P.create_sync_message(self.doc)

    async def handle(
        self,
        *,
        sender,
        user_id: int,
        can_write: bool,
        message: bytes,
        persist: PersistFn,
    ) -> None:
        if P.is_sync_step1(message):
            # Read path: always answer with our state (a SYNC_STEP2 reply). A
            # malformed STEP1 body (truncated/garbage state vector) must be
            # dropped, not raised — symmetric with the write-path guard below,
            # and reachable by any connected member (the read path is ungated).
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
            if not can_write:
                return  # silently drop — non-writer cannot mutate the room
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
                    await persist(user_id, update)
                except Exception as e:
                    self.poisoned = True
                    raise PersistError(str(self.key)) from e
            # Broadcast outside the lock (CRDT tolerates reordering; a slow
            # client must not block other writers).
            await self._broadcast(P.create_update_message(update), exclude=sender)
            return

        # AWARENESS and anything else: ignored in Slice 1 (presence is Slice 3).
        return

    async def _broadcast(self, frame: bytes, *, exclude) -> None:
        """Send to every client except `exclude`, isolating per-client failures:
        a broken/closed socket is dropped and the rest still receive the frame."""
        dead = []
        for client in list(self.clients):
            if client is exclude:
                continue
            try:
                await client.send_bytes(frame)
            except Exception:
                dead.append(client)
        for client in dead:
            self.clients.discard(client)


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

    async def acquire(self, key: RoomKey, *, load: LoadFn, client) -> Room:
        """Get-or-create the room, replaying persisted updates on cold start,
        and register `client`.

        The cold-start replay (`await load()`) runs OUTSIDE the manager-global
        lock, serialized only by a per-key lock — so hydrating one room never
        blocks connections to unrelated rooms (no process-wide head-of-line
        blocking as the update log grows).
        """
        async with self._lock:
            room = self._rooms.get(key)
            if room is not None:
                room.clients.add(client)
                return room
            keylock = self._keylocks.setdefault(key, asyncio.Lock())

        async with keylock:
            # Re-check under the per-key lock: another creator may have finished
            # while we waited.
            async with self._lock:
                room = self._rooms.get(key)
            if room is None:
                room = Room(key)
                for update in await load():  # runs OUTSIDE the manager lock
                    room.doc.apply_update(update)
                async with self._lock:
                    self._rooms[key] = room
                    room.clients.add(client)
                return room
            async with self._lock:
                room.clients.add(client)
            return room

    def get(self, key: RoomKey) -> Room | None:
        return self._rooms.get(key)

    async def release(self, key: RoomKey, *, client, on_evict=None) -> None:
        """Deregister a client; evict the room when the last one leaves. If a
        room is evicted and `on_evict` is given, call it (e.g. to checkpoint)
        OUTSIDE the manager lock — never hold the global lock across I/O."""
        async with self._lock:
            room = self._rooms.get(key)
            if room is None:
                return
            room.clients.discard(client)
            evicted = self._rooms.pop(key) if not room.clients else None
        if evicted is not None and on_evict is not None:
            await on_evict(evicted)

    async def evict(self, key: RoomKey) -> None:
        """Force-remove a room regardless of connected clients (poison recovery).
        The next acquire rebuilds it from the durable log."""
        async with self._lock:
            self._rooms.pop(key, None)


# Module-global registry (single-instance backend).
MANAGER = RoomManager()
