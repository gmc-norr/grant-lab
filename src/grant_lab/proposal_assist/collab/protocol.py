"""Thin, tested helpers over the y-sync protocol codec shipped in pycrdt.

We hand-roll the sync room rather than use pycrdt.websocket, so these helpers
classify inbound wire frames (to gate writes) and pull the raw Y update out of
a SYNC write frame (to persist it with an actor tag and re-broadcast it).

Wire format (pycrdt._sync):
  SYNC frame:      [YMessageType.SYNC (0)] [YSyncMessageType] [varuint len] [payload]
  AWARENESS frame: [YMessageType.AWARENESS (1)] [varuint len] [payload]
  DOC_EPOCH frame: [MESSAGE_DOC_EPOCH (4)] [varuint epoch]        (server → client only)
"""

from __future__ import annotations

from typing import NamedTuple

from pycrdt import (
    Awareness,
    YMessageType,
    YSyncMessageType,
    create_awareness_message,
    create_sync_message,
    create_update_message,
    handle_sync_message,
    write_var_uint,
)

__all__ = [
    "create_sync_message",
    "handle_sync_message",
    "create_update_message",
    "create_awareness_message",
    "MESSAGE_DOC_EPOCH",
    "create_doc_epoch_message",
    "read_doc_epoch_message",
    "Awareness",
    "is_sync_step1",
    "is_sync_write",
    "is_awareness",
    "extract_update",
    "extract_awareness_update",
    "safe_read_message",
    "guard_sync_frame",
    "read_awareness_client_ids",
    "read_awareness_entries",
    "filter_awareness_update",
    "AwarenessEntry",
]


# --- The one bounded varuint reader -----------------------------------------
#
# pycrdt's `read_var_uint` is an unbounded `while True` accumulating
# `uint |= (byte & 127) << shift`. On a run of continuation bytes (0x80+) that
# shifts a multi-megabit Python int once per byte: O(n^2). Measured on this
# codebase — 0.115 s at 32 KB, 1.77 s at 128 KB, 7.4 s at 256 KB, ~106 s at the
# 1,000,000-byte WebSocket frame cap. This project runs ONE asyncio loop under a
# single-instance constraint, so those seconds are the whole installation: every
# HTTP request, every other workspace's socket, every scheduler future.
#
# `f535f6c` fixed this once, inside `read_awareness_entries`, as a nested
# function. The reader stayed reachable through `handle_sync_message`,
# `extract_update` AND `extract_awareness_update` — including the awareness path
# that fix was written for, whose hardened entry parser never ran because the
# extractor in front of it blew up first. So the bound lives here, once, and
# `read_message` is not imported into this module at all: there is no door left
# to reach for.


def _read_var_uint(buf, pos: int) -> tuple[int, int]:
    """Decode one varuint from `buf` at `pos`; return (value, next position).

    Capped at 10 bytes — a 64-bit value is at most 10 base-128 groups — and
    bounds-checked on every byte, so a malformed frame raises PROMPTLY, in
    O(1), instead of shifting an ever-widening int.
    """
    n = len(buf)
    num = 0
    shift = 0
    for _ in range(10):
        if pos >= n:
            raise ValueError("y varuint: truncated")
        b = buf[pos]
        pos += 1
        num |= (b & 0x7F) << shift
        if b < 0x80:
            return num, pos
        shift += 7
    raise ValueError("y varuint: overlong")


# --- Document epoch frame (spec 2026-09-02-collab-doc-epoch-design.md §3.4) ---
#
# y-protocols uses 0 sync, 1 awareness, 2 auth, 3 query-awareness. 4 is free on
# both sides. The server sends one of these right before its STEP1 so the
# client can present the epoch back as `?epoch=N` on every reconnect; a Y.Doc
# from a previous document (a branch switch truncated the log) is then refused
# before it can send its state. Inbound, Room.handle ignores the type.
MESSAGE_DOC_EPOCH = 4


def create_doc_epoch_message(epoch: int) -> bytes:
    """[varuint 4][varuint epoch], written with pycrdt's own varuint writer."""
    if epoch < 0:
        raise ValueError("doc epoch must be non-negative")
    return write_var_uint(MESSAGE_DOC_EPOCH) + write_var_uint(epoch)


def read_doc_epoch_message(message: bytes) -> int:
    """The epoch carried by a DOC_EPOCH frame. Raises ValueError for any other
    type, a truncated or overlong varuint, or trailing bytes."""
    if len(message) < 1 or message[0] != MESSAGE_DOC_EPOCH:
        raise ValueError("not a doc-epoch frame")
    epoch, pos = _read_var_uint(message, 1)
    if pos != len(message):
        raise ValueError("doc-epoch frame: trailing bytes")
    return epoch


def safe_read_message(stream: bytes, start: int = 0) -> bytes:
    """Bounded replacement for `pycrdt.read_message`: the varuint-prefixed
    payload beginning at `start`.

    Two differences from pycrdt's reader, both deliberate. The length varuint is
    bounded (above). And a declared length the frame cannot satisfy RAISES,
    where pycrdt slices past the end and silently returns a short message —
    a truncated frame is malformed, and the callers all drop malformed frames.
    """
    length, pos = _read_var_uint(stream, start)
    if length > len(stream) - pos:
        raise ValueError("y frame: declared length exceeds the payload")
    return stream[pos : pos + length]


def guard_sync_frame(message: bytes) -> None:
    """Raise unless a SYNC frame's varuint-prefixed body is well-formed.

    `pycrdt.handle_sync_message` reads that varuint with the UNBOUNDED reader,
    and reimplementing its STEP1/STEP2/UPDATE dispatch here would fork the
    protocol. So the room calls this first: it proves — over a memoryview, no
    copy — that the varuint is at most 10 bytes and in range, after which
    pycrdt re-reads the same short varuint in microseconds.
    """
    body = memoryview(message)
    length, pos = _read_var_uint(body, 2)
    if length > len(body) - pos:
        raise ValueError("y frame: declared length exceeds the payload")


def is_awareness(message: bytes) -> bool:
    return len(message) >= 1 and message[0] == YMessageType.AWARENESS


def is_sync_step1(message: bytes) -> bool:
    return (
        len(message) >= 2
        and message[0] == YMessageType.SYNC
        and message[1] == YSyncMessageType.SYNC_STEP1
    )


def is_sync_write(message: bytes) -> bool:
    """True for SYNC frames that carry document changes (STEP2 or UPDATE).

    These are the frames a non-writer connection must not be allowed to apply,
    persist, or broadcast.
    """
    return (
        len(message) >= 2
        and message[0] == YMessageType.SYNC
        and message[1] in (YSyncMessageType.SYNC_STEP2, YSyncMessageType.SYNC_UPDATE)
    )


def extract_update(message: bytes) -> bytes:
    """Return the raw Y update payload from a SYNC write frame.

    Strips the 2-byte [SYNC][subtype] header and reads the varuint-prefixed
    payload. Caller is responsible for checking is_sync_write() first.
    """
    return safe_read_message(message, 2)


def extract_awareness_update(message: bytes) -> bytes:
    """Raw awareness payload from an AWARENESS frame: strip the 1-byte type header,
    read the varuint-prefixed payload. Caller checks is_awareness() first.
    Mirror of extract_update() (which strips the 2-byte SYNC header)."""
    return safe_read_message(message, 1)


class AwarenessEntry(NamedTuple):
    """One entry of a y-protocols awareness update: the declared client id, the
    entry's full byte span [start:end) within the payload (clientID varuint +
    clock varuint + varString state), the start of the state bytes
    (payload[state_start:end] is the JSON document), and whether the state is
    the JSON literal null — a removal; JSON.stringify(null) === "null", so a
    4-byte compare on the span already in hand, no JSON decode."""

    client_id: int
    start: int
    end: int
    state_start: int
    is_null: bool


def read_awareness_entries(payload: bytes) -> list[AwarenessEntry]:
    """Parse the entries of a y-protocols awareness update WITHOUT mutating any
    Awareness. Update format (encodeAwarenessUpdate): a varuint count, then per
    client [clientID varuint][clock varuint][state varString]. Records each
    entry's byte span so a filter can rebuild a payload from original bytes.

    HARDENED against adversarial frames (f535f6c, generalized): every varuint goes
    through the module's single bounded `_read_var_uint`, and every read is
    bounds-checked against the payload, so a malformed/oversized frame — e.g. a
    long run of 0x80+ continuation bytes — raises PROMPTLY (O(payload)) instead of
    shifting a multi-megabit Python int. See that reader for the measured cost of
    the unbounded version. The sole caller (room.handle) wraps this in try/except
    and drops the frame on any exception."""
    n = len(payload)
    pos = 0
    count, pos = _read_var_uint(payload, pos)
    entries: list[AwarenessEntry] = []
    # A conforming frame declares >= 3 bytes per client (id+clock+len), so `count`
    # cannot exceed `n`; a lying count simply raises "truncated" once the body is
    # exhausted (each iteration advances pos by >= 3), keeping this O(n).
    for _ in range(count):
        start = pos
        client_id, pos = _read_var_uint(payload, pos)
        _, pos = _read_var_uint(payload, pos)          # clock (skip)
        state_len, pos = _read_var_uint(payload, pos)  # varString byte length
        if state_len > n - pos:       # never skip past the payload
            raise ValueError("awareness varString: out of range")
        is_null = state_len == 4 and payload[pos : pos + 4] == b"null"
        state_start = pos
        pos += state_len              # skip state bytes
        entries.append(AwarenessEntry(client_id, start, pos, state_start, is_null))
    return entries


def read_awareness_client_ids(payload: bytes) -> list[int]:
    """The client ids declared in an awareness update — thin wrapper over
    read_awareness_entries (one parser, no drift)."""
    return [e.client_id for e in read_awareness_entries(payload)]


def filter_awareness_update(
    payload: bytes, entries: list[AwarenessEntry], kept: list[AwarenessEntry]
) -> bytes | None:
    """Rebuild the payload keeping only the `kept` subset of `entries` (the
    caller's already-partitioned allow-list — passing the entries themselves,
    not a derived id-set, keeps one source of truth).

    All kept -> the payload UNCHANGED, byte-identical (fast path — the
    overwhelmingly common case; zero re-encode in normal operation). None
    kept -> None (nothing to relay). Mixed -> rebuilt as varuint(kept count)
    + the kept entries' ORIGINAL byte spans — entries are never re-encoded,
    only re-counted. (write_var_uint is pycrdt's writer — byte-identical
    mirror of the reader in read_awareness_entries.)"""
    if len(kept) == len(entries):
        return payload
    if not kept:
        return None
    return write_var_uint(len(kept)) + b"".join(payload[e.start : e.end] for e in kept)
