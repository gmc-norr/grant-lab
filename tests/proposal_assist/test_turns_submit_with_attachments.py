"""B7 Task 3 — ``submit_user_turn`` with attachment_ids.

These tests cover the attachment branch of
:func:`call_manager.proposal_assist.turns.submit.submit_user_turn`,
including the NULL-tokens_estimate re-probe path that closes the
T2→T3 migration cap-bypass window (Codex review of plan rev1).
"""

from __future__ import annotations

import io
import uuid
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select

from call_manager.config import Settings
from call_manager.db.models import Attachment, ChatTurn
from call_manager.proposal_assist.attachments.storage import AttachmentStorage
from call_manager.proposal_assist.attachments.tokens import estimate_image_tokens
from call_manager.proposal_assist.turns.submit import submit_user_turn
from tests.proposal_assist.conftest import make_user, make_workspace


def _make_png_bytes(width: int = 200, height: int = 300) -> bytes:
    img = Image.new("RGB", (width, height), color=(128, 128, 128))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _seed_attachment(
    session,
    *,
    workspace,
    uploaded_by,
    tokens_estimate: int | None = None,
    width_px: int | None = None,
    height_px: int | None = None,
    storage_path: str | None = None,
) -> Attachment:
    aid = uuid.uuid4()
    att = Attachment(
        id=aid,
        workspace_id=workspace.id,
        kind="figure",
        filename="seed.png",
        storage_path=storage_path if storage_path is not None else f"{aid}.png",
        mime_type="image/png",
        uploaded_by=uploaded_by.id,
        width_px=width_px,
        height_px=height_px,
        tokens_estimate=tokens_estimate,
    )
    session.add(att)
    session.flush()
    return att


def _write_attachment_file(
    *,
    workspaces_dir: Path,
    workspace_id: uuid.UUID,
    attachment_id: uuid.UUID,
    suffix: str = ".png",
    width: int = 200,
    height: int = 300,
) -> None:
    storage = AttachmentStorage(root=workspaces_dir)
    storage.write_atomic(
        workspace_id=workspace_id,
        attachment_id=attachment_id,
        suffix=suffix,
        data=_make_png_bytes(width=width, height=height),
    )


def test_submit_with_one_attachment_builds_list_content(
    pg_session_factory,
    _settings_env,
    tmp_path,
):
    """A turn with one attachment stores content as a 2-element list and
    pre-counts tokens_in from the attachment's tokens_estimate + the
    text-length heuristic."""
    settings = Settings()
    with pg_session_factory() as s:
        user = make_user(s)
        ws = make_workspace(s, owner=user)
        # 200x300 → estimate_image_tokens(200, 300) = 80.
        att = _seed_attachment(
            s,
            workspace=ws,
            uploaded_by=user,
            tokens_estimate=80,
            width_px=200,
            height_px=300,
        )
        s.commit()
        uid, wsid, aid = user.id, ws.id, att.id

    text = "Look at this figure."  # len 20 → 20 // 4 = 5
    with pg_session_factory() as s:
        submit_user_turn(
            session=s,
            workspace_id=wsid,
            content_text=text,
            attachment_ids=[aid],
            submitted_by_user_id=uid,
            settings=settings,
            workspaces_dir=tmp_path,
        )
        s.commit()

    with pg_session_factory() as s:
        turn = s.scalar(
            select(ChatTurn).where(
                ChatTurn.workspace_id == wsid,
                ChatTurn.role == "user",
            )
        )

    assert isinstance(turn.content, list)
    assert turn.content == [
        {"type": "text", "text": text},
        {"type": "image", "attachment_id": str(aid)},
    ]
    # 80 (image) + 20 // 4 = 85.
    assert turn.tokens_in == 85


def test_submit_with_cross_workspace_attachment_raises(
    pg_session_factory,
    _settings_env,
    tmp_path,
):
    """An attachment_id referencing a row in a DIFFERENT workspace must
    fail loudly — never silently submit a turn with mismatched scope.
    """
    settings = Settings()
    with pg_session_factory() as s:
        owner_a = make_user(s, email="ws-a@ki.se")
        owner_b = make_user(s, email="ws-b@ki.se")
        ws_a = make_workspace(s, owner=owner_a)
        ws_b = make_workspace(s, owner=owner_b)
        att_b = _seed_attachment(s, workspace=ws_b, uploaded_by=owner_b, tokens_estimate=10)
        s.commit()
        owner_a_id = owner_a.id
        ws_a_id = ws_a.id
        cross_aid = att_b.id

    with pg_session_factory() as s:
        with pytest.raises(ValueError, match="does not belong"):
            submit_user_turn(
                session=s,
                workspace_id=ws_a_id,
                content_text="hi",
                attachment_ids=[cross_aid],
                submitted_by_user_id=owner_a_id,
                settings=settings,
                workspaces_dir=tmp_path,
            )
        s.rollback()


def test_submit_with_more_than_5_attachments_raises(
    pg_session_factory,
    _settings_env,
    tmp_path,
):
    """Hard cap: 5 images per turn. The validation runs BEFORE any DB
    lookup so we don't waste a SELECT on an over-quota request."""
    settings = Settings()
    with pg_session_factory() as s:
        user = make_user(s)
        ws = make_workspace(s, owner=user)
        s.commit()
        uid, wsid = user.id, ws.id

    six_ids = [uuid.uuid4() for _ in range(6)]
    with pg_session_factory() as s:
        with pytest.raises(ValueError, match="too many attachments"):
            submit_user_turn(
                session=s,
                workspace_id=wsid,
                content_text="hi",
                attachment_ids=six_ids,
                submitted_by_user_id=uid,
                settings=settings,
                workspaces_dir=tmp_path,
            )
        s.rollback()


def test_submit_null_tokens_estimate_re_probes_and_persists(
    pg_session_factory,
    _settings_env,
    tmp_path,
):
    """If an Attachment row was created by the T2 panel BEFORE T3's
    upload-handler backfill, ``tokens_estimate`` is NULL. Submit must
    re-probe the bytes from disk, persist the result onto the row,
    AND include it in the cap-counted tokens_in. NEVER default to zero
    — that would let an attacker bypass the cap via T2-window inserts.
    """
    settings = Settings()
    with pg_session_factory() as s:
        user = make_user(s)
        ws = make_workspace(s, owner=user)
        att = _seed_attachment(
            s,
            workspace=ws,
            uploaded_by=user,
            tokens_estimate=None,
            width_px=None,
            height_px=None,
        )
        s.commit()
        uid, wsid, aid = user.id, ws.id, att.id

    # Drop a 200x300 PNG at the path the storage helper expects.
    _write_attachment_file(
        workspaces_dir=tmp_path,
        workspace_id=wsid,
        attachment_id=aid,
        width=200,
        height=300,
    )

    with pg_session_factory() as s:
        submit_user_turn(
            session=s,
            workspace_id=wsid,
            content_text="probe please",
            attachment_ids=[aid],
            submitted_by_user_id=uid,
            settings=settings,
            workspaces_dir=tmp_path,
        )
        s.commit()

    expected_tokens = estimate_image_tokens(200, 300)  # 80
    with pg_session_factory() as s:
        att = s.get(Attachment, aid)
        turn = s.scalar(
            select(ChatTurn).where(
                ChatTurn.workspace_id == wsid,
                ChatTurn.role == "user",
            )
        )

    assert att.tokens_estimate == expected_tokens
    assert att.width_px == 200
    assert att.height_px == 300
    # 80 (image) + len("probe please") // 4 = 80 + 3 = 83.
    assert turn.tokens_in == expected_tokens + (len("probe please") // 4)


def test_submit_null_tokens_estimate_missing_file_raises(
    pg_session_factory,
    _settings_env,
    tmp_path,
):
    """If ``tokens_estimate`` is NULL AND the file is missing on disk
    (orphan row), submit MUST raise — never silently default to zero
    (which would bypass the daily-token cap)."""
    settings = Settings()
    with pg_session_factory() as s:
        user = make_user(s)
        ws = make_workspace(s, owner=user)
        att = _seed_attachment(
            s,
            workspace=ws,
            uploaded_by=user,
            tokens_estimate=None,
        )
        s.commit()
        uid, wsid, aid = user.id, ws.id, att.id

    # Deliberately do NOT write the file to disk.
    with pg_session_factory() as s:
        with pytest.raises(ValueError, match="file missing"):
            submit_user_turn(
                session=s,
                workspace_id=wsid,
                content_text="hi",
                attachment_ids=[aid],
                submitted_by_user_id=uid,
                settings=settings,
                workspaces_dir=tmp_path,
            )
        s.rollback()


def test_submit_without_attachments_keeps_dict_content_shape(
    pg_session_factory,
    _settings_env,
):
    """Backwards-compat: the legacy ``{"type":"text","text":...}``
    dict shape is preserved when no attachments accompany the turn so
    existing renderers / adapters / replays don't need to special-case
    a 1-element list."""
    settings = Settings()
    with pg_session_factory() as s:
        user = make_user(s)
        ws = make_workspace(s, owner=user)
        s.commit()
        uid, wsid = user.id, ws.id

    with pg_session_factory() as s:
        submit_user_turn(
            session=s,
            workspace_id=wsid,
            content_text="plain",
            submitted_by_user_id=uid,
            settings=settings,
        )
        s.commit()

    with pg_session_factory() as s:
        turn = s.scalar(
            select(ChatTurn).where(
                ChatTurn.workspace_id == wsid,
                ChatTurn.role == "user",
            )
        )
    assert turn.content == {"type": "text", "text": "plain"}


def test_submit_empty_text_with_no_attachments_raises(
    pg_session_factory, _settings_env
):
    """Empty submit (no text, no attachments) is rejected at submit
    entry with a clear ValueError — not a confusing downstream
    'attachment_resolution_failed: text block missing text'."""
    settings = Settings()
    with pg_session_factory() as s:
        owner = make_user(s)
        s.commit()
        ws = make_workspace(s, owner=owner)
        s.commit()
        wsid, owner_id = ws.id, owner.id

    with pg_session_factory() as s, pytest.raises(
        ValueError, match="turn must have text or attachments"
    ):
        submit_user_turn(
            session=s,
            workspace_id=wsid,
            content_text="",
            attachment_ids=None,
            submitted_by_user_id=owner_id,
            settings=settings,
        )


def test_submit_empty_text_with_attachments_raises(
    pg_session_factory, _settings_env, tmp_path
):
    """Empty text + at least one attachment: rejected at submit entry
    with a clearer message than the resolver's downstream 'missing
    text' error. The attachment_ids form field could be set by a
    composer bug or a misuse; surface the error at the boundary."""
    settings = Settings()
    aid = uuid.uuid4()
    with pg_session_factory() as s:
        owner = make_user(s)
        s.commit()
        ws = make_workspace(s, owner=owner)
        s.commit()
        wsid, owner_id = ws.id, owner.id

        # Seed an attachment with a real on-disk file so the resolver
        # would otherwise be able to load it — we want to prove that
        # we fail BEFORE reaching the resolver.
        att_dir = tmp_path / str(wsid) / "attachments"
        att_dir.mkdir(parents=True, exist_ok=True)
        img = Image.new("RGB", (4, 4), color=(0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        (att_dir / f"{aid}.png").write_bytes(buf.getvalue())
        s.add(
            Attachment(
                id=aid,
                workspace_id=wsid,
                kind="figure",
                filename="x.png",
                storage_path=f"{aid}.png",
                mime_type="image/png",
                uploaded_by=owner_id,
                width_px=4,
                height_px=4,
                tokens_estimate=1,
            )
        )
        s.commit()

    with pg_session_factory() as s, pytest.raises(
        ValueError, match="content_text required when attachments"
    ):
        submit_user_turn(
            session=s,
            workspace_id=wsid,
            content_text="",
            attachment_ids=[aid],
            submitted_by_user_id=owner_id,
            settings=settings,
            workspaces_dir=tmp_path,
        )
