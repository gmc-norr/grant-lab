"""submit_user_turn with reference_ids: validation, pre-count, and
combined inclusion cap. Follows the same pattern as
test_turns_submit_with_attachments.py."""

import uuid

import pytest
from sqlalchemy import select

from grant_lab.config import Settings
from grant_lab.db.models import (
    ChatTurn,
    Reference,
    User,
    Workspace,
)
from grant_lab.proposal_assist.turns.submit import submit_user_turn


def _seed_workspace(s, *, owner_email=None):
    owner = User(email=owner_email or f"u-{uuid.uuid4()}@umu.se")
    s.add(owner)
    s.flush()
    ws = Workspace(owner_id=owner.id, title="T")
    s.add(ws)
    s.flush()
    return owner, ws


def _seed_reference(
    s,
    *,
    workspace_id,
    text_status="done",
    text_chars_total=200,
    title="Test Paper",
    filename="paper.pdf",
):
    """Create a Reference row with the minimum required fields.

    Note: ``source_type`` must be one of the values allowed by the
    ck_references_source_type CHECK constraint
    ('uploaded','crossref','pubmed','arxiv','web'); we use 'uploaded'
    for the manual-upload case. ``embedding_model`` is NOT on Reference
    — that column lives on ReferenceChunk in the Slice 1 schema.
    """
    ref = Reference(
        workspace_id=workspace_id,
        kind="paper",
        title=title,
        filename=filename,
        storage_path=f"references/{uuid.uuid4()}/original.pdf",
        mime_type="application/pdf",
        source_type="uploaded",
        source_uri=None,
        uploaded_by=None,
        text_status=text_status,
        text_chars_total=text_chars_total,
    )
    s.add(ref)
    s.flush()
    return ref


def test_submit_with_one_reference_sets_pre_count(monkeypatch, pg_session_factory, tmp_path):
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("WORKSPACES_DIR", str(tmp_path))
    settings = Settings.model_construct(workspaces_dir=str(tmp_path))

    with pg_session_factory() as s:
        owner, ws = _seed_workspace(s)
        ref = _seed_reference(s, workspace_id=ws.id, text_chars_total=400)
        s.commit()
        wsid, refid, uid = ws.id, ref.id, owner.id

    with pg_session_factory() as s:
        submit_user_turn(
            session=s,
            workspace_id=wsid,
            content_text="discuss this paper",
            reference_ids=[refid],
            submitted_by_user_id=uid,
            settings=settings,
            workspaces_dir=tmp_path,
        )
        s.commit()

    with pg_session_factory() as s:
        ct = s.scalar(
            select(ChatTurn).where(ChatTurn.workspace_id == wsid, ChatTurn.role == "user")
        )
    # 400 // 4 (reference) + len("discuss this paper") // 4 (content) = 100 + 4 = 104
    assert ct.tokens_in == 100 + (len("discuss this paper") // 4)
    # Content should be a list with text + reference blocks
    assert isinstance(ct.content, list)
    assert any(
        b.get("type") == "reference" and b.get("reference_id") == str(refid) for b in ct.content
    )


def test_submit_rejects_reference_with_text_status_pending(
    monkeypatch, pg_session_factory, tmp_path
):
    """Including a not-yet-extracted reference is a contract violation —
    Phase 2 hasn't produced text.txt yet, so the resolver would fail."""
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    settings = Settings.model_construct(workspaces_dir=str(tmp_path))

    with pg_session_factory() as s:
        owner, ws = _seed_workspace(s)
        ref = _seed_reference(s, workspace_id=ws.id, text_status="pending")
        s.commit()
        wsid, refid, uid = ws.id, ref.id, owner.id

    with pg_session_factory() as s, pytest.raises(ValueError, match="not ready"):
        submit_user_turn(
            session=s,
            workspace_id=wsid,
            content_text="hi",
            reference_ids=[refid],
            submitted_by_user_id=uid,
            settings=settings,
            workspaces_dir=tmp_path,
        )


def test_submit_rejects_cross_workspace_reference(monkeypatch, pg_session_factory, tmp_path):
    """IDOR defense: ref belongs to workspace B; user submits a turn in
    workspace A. Must reject."""
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    settings = Settings.model_construct(workspaces_dir=str(tmp_path))

    with pg_session_factory() as s:
        owner_a, ws_a = _seed_workspace(s)
        owner_b, ws_b = _seed_workspace(s)
        ref_b = _seed_reference(s, workspace_id=ws_b.id)
        s.commit()
        ws_a_id, ref_b_id, uid_a = ws_a.id, ref_b.id, owner_a.id

    with pg_session_factory() as s, pytest.raises(ValueError, match="does not belong"):
        submit_user_turn(
            session=s,
            workspace_id=ws_a_id,
            content_text="hi",
            reference_ids=[ref_b_id],
            submitted_by_user_id=uid_a,
            settings=settings,
            workspaces_dir=tmp_path,
        )


def test_submit_rejects_combined_inclusions_exceeding_cap(
    monkeypatch, pg_session_factory, tmp_path
):
    """5 attachments + 1 reference > MAX_TURN_INCLUSIONS=5."""
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    settings = Settings.model_construct(workspaces_dir=str(tmp_path))

    with pg_session_factory() as s:
        owner, ws = _seed_workspace(s)
        # Fake attachment IDs — submit_user_turn rejects them as missing
        # but the inclusion-count check should fire FIRST.
        ref = _seed_reference(s, workspace_id=ws.id)
        s.commit()
        wsid, refid, uid = ws.id, ref.id, owner.id

    # 5 fake attachment ids + 1 real reference
    fake_atts = [uuid.uuid4() for _ in range(5)]
    with pg_session_factory() as s, pytest.raises(ValueError, match="too many inclusions"):
        submit_user_turn(
            session=s,
            workspace_id=wsid,
            content_text="hi",
            attachment_ids=fake_atts,
            reference_ids=[refid],
            submitted_by_user_id=uid,
            settings=settings,
            workspaces_dir=tmp_path,
        )


def test_submit_with_null_text_chars_treated_as_zero(monkeypatch, pg_session_factory, tmp_path):
    """Defensive: a reference with text_status='done' but text_chars_total
    NULL (e.g., extracted-but-not-counted edge case) is pre-counted as 0.
    The submit doesn't fail — the resolver would still find text.txt.
    Worst case: cap pre-count understates by O(50K chars / ref); the
    SDK's TurnDoneEvent corrects the running total."""
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    settings = Settings.model_construct(workspaces_dir=str(tmp_path))

    with pg_session_factory() as s:
        owner, ws = _seed_workspace(s)
        ref = _seed_reference(s, workspace_id=ws.id, text_chars_total=None)
        s.commit()
        wsid, refid, uid = ws.id, ref.id, owner.id

    with pg_session_factory() as s:
        submit_user_turn(
            session=s,
            workspace_id=wsid,
            content_text="hi",
            reference_ids=[refid],
            submitted_by_user_id=uid,
            settings=settings,
            workspaces_dir=tmp_path,
        )
        s.commit()

    with pg_session_factory() as s:
        ct = s.scalar(
            select(ChatTurn).where(ChatTurn.workspace_id == wsid, ChatTurn.role == "user")
        )
    # tokens_in = 0 (null ref) + len("hi") // 4 = 0
    assert ct.tokens_in == 0
