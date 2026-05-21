"""Resolve ChatTurn.content into a form the SDK adapter can send.

ChatTurn.content stores image blocks as {"type": "image", "attachment_id": "..."} —
lightweight references. Before a turn runs, the processor calls
resolve_user_content to swap these references for full API-shape image
blocks with base64-encoded bytes, after validating that each attachment
exists, belongs to this workspace, and the file is present on disk.

This module owns ALL attachment-related DB and filesystem access for the
turn-run path. The Claude adapter stays ignorant of both.
"""

from __future__ import annotations

import base64
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.db.models import Attachment

_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


class AttachmentResolutionError(RuntimeError):
    """Raised when an attachment_id cannot be resolved — missing row,
    wrong workspace, missing file on disk, or unknown content-block
    type. The processor catches this, marks the turn failed, and
    emits cloud_turn_failed. Never silently dropped."""


def resolve_user_content(
    *,
    session: Session,
    workspaces_dir: Path,
    workspace_id: uuid.UUID,
    content: Any,
) -> str | list[dict[str, Any]]:
    """Convert stored ChatTurn.content to a form the adapter can send.

    - str → str (cheap path).
    - {"type": "text", "text": ...} → str (cheap path).
    - list of blocks → list[dict] with image blocks fully resolved to
      base64 sources. If after resolution only text blocks remain and
      there is exactly one, collapse to str.
    - Anything else → raise AttachmentResolutionError.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("type") == "text":
            return str(content.get("text", ""))
        raise AttachmentResolutionError(
            f"unexpected single-block content type: {content.get('type')!r}"
        )
    if not isinstance(content, list):
        raise AttachmentResolutionError(
            f"unexpected content type: {type(content).__name__}"
        )

    out: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict) or "type" not in block:
            raise AttachmentResolutionError(f"malformed content block: {block!r}")
        kind = block["type"]
        if kind == "text":
            text = block.get("text")
            if not isinstance(text, str) or not text:
                raise AttachmentResolutionError("text block missing text")
            out.append({"type": "text", "text": text})
        elif kind == "image":
            aid_str = block.get("attachment_id")
            if not isinstance(aid_str, str):
                raise AttachmentResolutionError("image block missing attachment_id")
            try:
                aid = uuid.UUID(aid_str)
            except ValueError as e:
                raise AttachmentResolutionError(
                    f"image block attachment_id not a valid uuid: {aid_str!r}"
                ) from e
            att = session.scalar(select(Attachment).where(Attachment.id == aid))
            if att is None:
                raise AttachmentResolutionError(f"attachment {aid} not found")
            if att.workspace_id != workspace_id:
                raise AttachmentResolutionError(
                    f"attachment {aid} belongs to workspace {att.workspace_id}, "
                    f"not {workspace_id}"
                )
            file_path = (
                workspaces_dir
                / str(workspace_id)
                / "attachments"
                / Path(att.storage_path).name
            )
            if not file_path.exists():
                raise AttachmentResolutionError(
                    f"attachment {aid} file missing on disk at {file_path}"
                )
            suffix = file_path.suffix.lower()
            if suffix not in _MIME_BY_SUFFIX:
                raise AttachmentResolutionError(
                    f"attachment {aid} has unsupported suffix {suffix!r}"
                )
            data = file_path.read_bytes()
            out.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": _MIME_BY_SUFFIX[suffix],
                        "data": base64.b64encode(data).decode("ascii"),
                    },
                }
            )
        else:
            raise AttachmentResolutionError(f"unknown content-block type: {kind!r}")

    if not out:
        raise AttachmentResolutionError("content list resolved to zero blocks")
    if len(out) == 1 and out[0]["type"] == "text":
        return out[0]["text"]
    return out
