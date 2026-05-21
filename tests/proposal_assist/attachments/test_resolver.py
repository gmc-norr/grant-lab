"""Resolver: resolve ChatTurn.content blocks (attachment-id references) into
Anthropic API-shape content blocks with base64-encoded bytes.

Uses real Postgres via pg_session_factory. Attachment files are written to
tmp_path/<wsid>/attachments/<aid>.png so the resolver's path derivation
(Path(att.storage_path).name) works with a relative storage_path value.
"""

from __future__ import annotations

import base64
import io
import uuid
from pathlib import Path

import pytest
from PIL import Image

from call_manager.db.models import Attachment, User, Workspace
from call_manager.proposal_assist.attachments.resolver import (
    AttachmentResolutionError,
    resolve_user_content,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_png_bytes(width: int = 4, height: int = 4) -> bytes:
    img = Image.new("RGB", (width, height), color=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _seed_user_and_workspace(session) -> tuple[User, Workspace]:
    """Insert a User + Workspace and return both ORM objects (flushed but not
    committed so callers can add more objects before committing)."""
    u = User(email=f"resolver-test-{uuid.uuid4().hex[:8]}@ki.se")
    session.add(u)
    session.flush()
    ws = Workspace(title="test ws", owner_id=u.id)
    session.add(ws)
    session.flush()
    return u, ws


def _seed_attachment(
    session,
    workspace: Workspace,
    user: User,
    aid: uuid.UUID,
    storage_path: str,
) -> Attachment:
    att = Attachment(
        id=aid,
        workspace_id=workspace.id,
        kind="figure",
        filename=f"{aid}.png",
        storage_path=storage_path,
        mime_type="image/png",
        uploaded_by=user.id,
    )
    session.add(att)
    session.flush()
    return att


def _write_fake_file(tmp_path: Path, ws_id: uuid.UUID, aid: uuid.UUID) -> Path:
    """Write a small PNG to tmp_path/<ws_id>/attachments/<aid>.png."""
    att_dir = tmp_path / str(ws_id) / "attachments"
    att_dir.mkdir(parents=True, exist_ok=True)
    file_path = att_dir / f"{aid}.png"
    file_path.write_bytes(_make_png_bytes())
    return file_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_resolve_str_passthrough(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        result = resolve_user_content(
            session=s,
            workspaces_dir=tmp_path,
            workspace_id=uuid.uuid4(),
            content="hello world",
        )
    assert result == "hello world"


def test_resolve_single_text_dict_returns_str(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        result = resolve_user_content(
            session=s,
            workspaces_dir=tmp_path,
            workspace_id=uuid.uuid4(),
            content={"type": "text", "text": "plain text block"},
        )
    assert result == "plain text block"


def test_resolve_list_with_text_and_image(pg_session_factory, tmp_path):
    """A list with one text block + one image block resolves to a list[dict]
    with the image block replaced by the base64 API shape."""
    aid = uuid.uuid4()
    with pg_session_factory() as s:
        u, ws = _seed_user_and_workspace(s)
        # Use a relative path — the resolver only uses .name from it.
        _seed_attachment(s, ws, u, aid, storage_path=f"relative/{aid}.png")
        s.commit()
        ws_id = ws.id

    _write_fake_file(tmp_path, ws_id, aid)

    content = [
        {"type": "text", "text": "What is in this figure?"},
        {"type": "image", "attachment_id": str(aid)},
    ]
    with pg_session_factory() as s:
        result = resolve_user_content(
            session=s,
            workspaces_dir=tmp_path,
            workspace_id=ws_id,
            content=content,
        )

    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0] == {"type": "text", "text": "What is in this figure?"}
    img_block = result[1]
    assert img_block["type"] == "image"
    assert img_block["source"]["type"] == "base64"
    assert img_block["source"]["media_type"] == "image/png"
    # Round-trip the base64 back to verify it's the file we wrote.
    decoded = base64.b64decode(img_block["source"]["data"])
    assert decoded == _make_png_bytes()


def test_resolve_list_collapses_single_text_to_str(pg_session_factory, tmp_path):
    """A list with only one text block should collapse to a plain str."""
    with pg_session_factory() as s:
        result = resolve_user_content(
            session=s,
            workspaces_dir=tmp_path,
            workspace_id=uuid.uuid4(),
            content=[{"type": "text", "text": "just text"}],
        )
    assert result == "just text"


def test_resolve_missing_attachment_raises(pg_session_factory, tmp_path):
    """An attachment_id that has no DB row raises AttachmentResolutionError."""
    missing_aid = uuid.uuid4()
    with (
        pg_session_factory() as s,
        pytest.raises(AttachmentResolutionError, match=str(missing_aid)),
    ):
        resolve_user_content(
            session=s,
            workspaces_dir=tmp_path,
            workspace_id=uuid.uuid4(),
            content=[{"type": "image", "attachment_id": str(missing_aid)}],
        )


def test_resolve_wrong_workspace_raises(pg_session_factory, tmp_path):
    """An attachment_id that belongs to a different workspace raises."""
    aid = uuid.uuid4()
    with pg_session_factory() as s:
        u, ws = _seed_user_and_workspace(s)
        _seed_attachment(s, ws, u, aid, storage_path=f"{aid}.png")
        s.commit()

    other_ws_id = uuid.uuid4()
    with (
        pg_session_factory() as s,
        pytest.raises(AttachmentResolutionError, match="belongs to workspace"),
    ):
        resolve_user_content(
            session=s,
            workspaces_dir=tmp_path,
            workspace_id=other_ws_id,
            content=[{"type": "image", "attachment_id": str(aid)}],
        )


def test_resolve_missing_file_on_disk_raises(pg_session_factory, tmp_path):
    """An attachment whose DB row exists but file is missing on disk raises."""
    aid = uuid.uuid4()
    with pg_session_factory() as s:
        u, ws = _seed_user_and_workspace(s)
        _seed_attachment(s, ws, u, aid, storage_path=f"{aid}.png")
        s.commit()
        ws_id = ws.id

    # Deliberately do NOT write the file to disk.
    with (
        pg_session_factory() as s,
        pytest.raises(AttachmentResolutionError, match="missing on disk"),
    ):
        resolve_user_content(
            session=s,
            workspaces_dir=tmp_path,
            workspace_id=ws_id,
            content=[{"type": "image", "attachment_id": str(aid)}],
        )


def test_resolve_unknown_block_type_raises(pg_session_factory, tmp_path):
    """A content block with an unrecognized type raises AttachmentResolutionError."""
    with (
        pg_session_factory() as s,
        pytest.raises(AttachmentResolutionError, match="unknown content-block type"),
    ):
        resolve_user_content(
            session=s,
            workspaces_dir=tmp_path,
            workspace_id=uuid.uuid4(),
            content=[{"type": "video", "url": "http://example.com/video.mp4"}],
        )


def test_resolve_malformed_block_no_type_raises(pg_session_factory, tmp_path):
    """A block dict missing the `type` key raises."""
    with (
        pg_session_factory() as s,
        pytest.raises(AttachmentResolutionError, match="malformed content block"),
    ):
        resolve_user_content(
            session=s,
            workspaces_dir=tmp_path,
            workspace_id=uuid.uuid4(),
            content=[{"foo": "bar"}],
        )


def test_resolve_text_block_empty_text_raises(pg_session_factory, tmp_path):
    """A text block with empty `text` raises rather than silently emitting an empty block."""
    with (
        pg_session_factory() as s,
        pytest.raises(AttachmentResolutionError, match="text block missing text"),
    ):
        resolve_user_content(
            session=s,
            workspaces_dir=tmp_path,
            workspace_id=uuid.uuid4(),
            content=[{"type": "text", "text": ""}],
        )


def test_resolve_image_block_missing_attachment_id_raises(pg_session_factory, tmp_path):
    """An image block without `attachment_id` raises."""
    with (
        pg_session_factory() as s,
        pytest.raises(AttachmentResolutionError, match="image block missing attachment_id"),
    ):
        resolve_user_content(
            session=s,
            workspaces_dir=tmp_path,
            workspace_id=uuid.uuid4(),
            content=[{"type": "image"}],
        )


def test_resolve_image_block_invalid_uuid_raises(pg_session_factory, tmp_path):
    """An image block whose attachment_id is not a valid UUID raises."""
    with (
        pg_session_factory() as s,
        pytest.raises(AttachmentResolutionError, match="not a valid uuid"),
    ):
        resolve_user_content(
            session=s,
            workspaces_dir=tmp_path,
            workspace_id=uuid.uuid4(),
            content=[{"type": "image", "attachment_id": "not-a-uuid"}],
        )


def test_resolve_unsupported_suffix_raises(pg_session_factory, tmp_path):
    """An attachment whose on-disk file has an unsupported extension raises."""
    aid = uuid.uuid4()
    with pg_session_factory() as s:
        u, ws = _seed_user_and_workspace(s)
        _seed_attachment(s, ws, u, aid, storage_path=f"{aid}.bmp")
        s.commit()
        ws_id = ws.id

    # Write a .bmp file (which the resolver's _MIME_BY_SUFFIX rejects).
    att_dir = tmp_path / str(ws_id) / "attachments"
    att_dir.mkdir(parents=True, exist_ok=True)
    (att_dir / f"{aid}.bmp").write_bytes(b"BM_fake_bmp_content")

    with (
        pg_session_factory() as s,
        pytest.raises(AttachmentResolutionError, match="unsupported suffix"),
    ):
        resolve_user_content(
            session=s,
            workspaces_dir=tmp_path,
            workspace_id=ws_id,
            content=[{"type": "image", "attachment_id": str(aid)}],
        )
