"""Thin, tested helpers over the y-sync protocol codec shipped in pycrdt.

We hand-roll the sync room rather than use pycrdt.websocket, so these helpers
classify inbound wire frames (to gate writes) and pull the raw Y update out of
a SYNC write frame (to persist it with an actor tag and re-broadcast it).

Wire format (pycrdt._sync):
  SYNC frame:      [YMessageType.SYNC (0)] [YSyncMessageType] [varuint len] [payload]
  AWARENESS frame: [YMessageType.AWARENESS (1)] [varuint len] [payload]
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
    "read_awareness_entries",
    "filter_awareness_update",
    "AwarenessEntry",
]


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
    return read_message(message[2:])


def extract_awareness_update(message: bytes) -> bytes:
    """Raw awareness payload from an AWARENESS frame: strip the 1-byte type header,
    read the varuint-prefixed payload. Caller checks is_awareness() first.
    Mirror of extract_update() (which strips the 2-byte SYNC header)."""
    return read_message(message[1:])


class AwarenessEntry(NamedTuple):
    """One entry of a y-protocols awareness update: the declared client id, the
    entry's full byte span [start:end) within the payload (clientID varuint +
    clock varuint + varString state), and whether the state is the JSON literal
    null — a removal; JSON.stringify(null) === "null", so a 4-byte compare on
    the span already in hand, no JSON decode."""

    client_id: int
    start: int
    end: int
    is_null: bool


def read_awareness_entries(payload: bytes) -> list[AwarenessEntry]:
    """Parse the entries of a y-protocols awareness update WITHOUT mutating any
    Awareness. Update format (encodeAwarenessUpdate): a varuint count, then per
    client [clientID varuint][clock varuint][state varString]. Records each
    entry's byte span so a filter can rebuild a payload from original bytes.

    HARDENED against adversarial frames (f535f6c): every varuint is capped at 10
    bytes (a 64-bit value is at most 10 base-128 groups) and every read is
    bounds-checked against the payload, so a malformed/oversized frame — e.g. a
    long run of 0x80+ continuation bytes — raises PROMPTLY (O(payload)) instead of
    shifting a multi-megabit Python int. Without this cap a ~1 MB all-continuation
    payload (under the WS frame cap) is an O(n^2) CPU stall that blocks the entire
    single-instance asyncio event loop — a whole-process DoS, since the awareness
    path is ungated by can_write. The sole caller (room.handle) wraps this in
    try/except and drops the frame on any exception."""
    n = len(payload)
    pos = 0

    def _rvu() -> int:
        nonlocal pos
        num = 0
        shift = 0
        for _ in range(10):  # a 64-bit varuint is at most 10 base-128 bytes
            if pos >= n:
                raise ValueError("awareness varuint: truncated")
            b = payload[pos]
            pos += 1
            num |= (b & 0x7F) << shift
            if b < 0x80:
                return num
            shift += 7
        raise ValueError("awareness varuint: overlong")

    count = _rvu()
    entries: list[AwarenessEntry] = []
    # A conforming frame declares >= 3 bytes per client (id+clock+len), so `count`
    # cannot exceed `n`; a lying count simply raises "truncated" once the body is
    # exhausted (each iteration advances pos by >= 3), keeping this O(n).
    for _ in range(count):
        start = pos
        client_id = _rvu()
        _rvu()                        # clock (skip)
        state_len = _rvu()            # varString byte length
        if state_len > n - pos:       # never skip past the payload
            raise ValueError("awareness varString: out of range")
        is_null = state_len == 4 and payload[pos : pos + 4] == b"null"
        pos += state_len              # skip state bytes
        entries.append(AwarenessEntry(client_id, start, pos, is_null))
    return entries


def read_awareness_client_ids(payload: bytes) -> list[int]:
    """The client ids declared in an awareness update — thin wrapper over
    read_awareness_entries (one parser, no drift)."""
    return [e.client_id for e in read_awareness_entries(payload)]


def _write_varuint(num: int) -> bytes:
    """Base-128 varuint writer — mirror of the reader in read_awareness_entries."""
    out = bytearray()
    while num > 0x7F:
        out.append(0x80 | (num & 0x7F))
        num >>= 7
    out.append(num)
    return bytes(out)


def filter_awareness_update(
    payload: bytes, entries: list[AwarenessEntry], allowed_ids: set[int]
) -> bytes | None:
    """Drop the entries whose client_id is not in allowed_ids.

    All allowed -> the payload UNCHANGED, byte-identical (fast path — the
    overwhelmingly common case; zero re-encode in normal operation). None
    allowed -> None (nothing to relay). Mixed -> rebuilt as varuint(kept count)
    + the kept entries' ORIGINAL byte spans — entries are never re-encoded,
    only re-counted."""
    kept = [e for e in entries if e.client_id in allowed_ids]
    if len(kept) == len(entries):
        return payload
    if not kept:
        return None
    return _write_varuint(len(kept)) + b"".join(payload[e.start : e.end] for e in kept)
