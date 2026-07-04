"""Thin, tested helpers over the y-sync protocol codec shipped in pycrdt.

We hand-roll the sync room rather than use pycrdt.websocket, so these helpers
classify inbound wire frames (to gate writes) and pull the raw Y update out of
a SYNC write frame (to persist it with an actor tag and re-broadcast it).

Wire format (pycrdt._sync):
  SYNC frame:      [YMessageType.SYNC (0)] [YSyncMessageType] [varuint len] [payload]
  AWARENESS frame: [YMessageType.AWARENESS (1)] [varuint len] [payload]
"""

from __future__ import annotations

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
