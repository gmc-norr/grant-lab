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
    YMessageType,
    YSyncMessageType,
    create_sync_message,
    create_update_message,
    handle_sync_message,
    read_message,
)

__all__ = [
    "create_sync_message",
    "handle_sync_message",
    "create_update_message",
    "is_sync_step1",
    "is_sync_write",
    "is_awareness",
    "extract_update",
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
