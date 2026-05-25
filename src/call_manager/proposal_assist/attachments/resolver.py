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

from call_manager.db.models import Attachment, Reference

_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

REFERENCE_TEXT_BUDGET_CHARS = 50_000
"""Per-reference truncation budget for content injection. Must match
``call_manager.proposal_assist.turns.submit.REFERENCE_TEXT_BUDGET_CHARS``
— the submit pre-count uses this value to estimate tokens. Keep in
sync; the constants are duplicated rather than imported to avoid a
cyclic module dep (submit imports resolver context indirectly via
processor)."""


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
        raise AttachmentResolutionError(f"unexpected content type: {type(content).__name__}")

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
                    f"attachment {aid} belongs to workspace {att.workspace_id}, not {workspace_id}"
                )
            file_path = (
                workspaces_dir / str(workspace_id) / "attachments" / Path(att.storage_path).name
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
        elif kind == "reference":
            rid_str = block.get("reference_id")
            if not isinstance(rid_str, str):
                raise AttachmentResolutionError("reference block missing reference_id")
            try:
                rid = uuid.UUID(rid_str)
            except ValueError as e:
                raise AttachmentResolutionError(
                    f"reference block reference_id not a valid uuid: {rid_str!r}"
                ) from e
            ref = session.scalar(select(Reference).where(Reference.id == rid))
            # IDOR: collapse "not found" + "wrong workspace" into the same
            # error message so callers can't probe ref ids across tenants.
            # Mirrors the pattern in
            # ``call_manager.proposal_assist.turns.submit``.
            if ref is None or ref.workspace_id != workspace_id:
                raise AttachmentResolutionError(
                    f"reference {rid} does not belong to workspace {workspace_id}"
                )
            if ref.text_status != "done":
                raise AttachmentResolutionError(
                    f"reference {rid} text not ready (text_status={ref.text_status!r})"
                )
            text_path = workspaces_dir / str(workspace_id) / "references" / str(rid) / "text.txt"
            if not text_path.exists():
                raise AttachmentResolutionError(f"reference {rid} text file missing at {text_path}")
            full_text = text_path.read_text(errors="replace")
            display_label = ref.title or ref.filename
            if len(full_text) > REFERENCE_TEXT_BUDGET_CHARS:
                body = (
                    full_text[:REFERENCE_TEXT_BUDGET_CHARS]
                    + "\n\n[truncated — full text via search_references / "
                    "read_reference_section MCP tools]"
                )
            else:
                body = full_text
            wrapped = f"<system>Reference: {display_label}\n\n{body}\n</system>"
            out.append({"type": "text", "text": wrapped})
        else:
            raise AttachmentResolutionError(f"unknown content-block type: {kind!r}")

    if not out:
        raise AttachmentResolutionError("content list resolved to zero blocks")
    if len(out) == 1 and out[0]["type"] == "text":
        return out[0]["text"]
    return out
