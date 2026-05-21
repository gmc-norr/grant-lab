"""Tests for POST /workspaces/{id}/turns — B6 Task 6 + B7 Task 3.

H1: response body contains the rendered user-turn row AND the SSE-connector div.
H2: user-supplied content is HTML-escaped before being embedded in the response.

B7 additions (T3):
- multipart upload via ``files=`` form field is persisted + linked to the turn.
- ``attachment_ids=`` form field references existing rows.
- viewer cannot submit a turn (shared-read / single-write).
"""

from __future__ import annotations

import io
import uuid

from PIL import Image


def _png_bytes(width: int = 16, height: int = 16) -> bytes:
    img = Image.new("RGB", (width, height), color=(0, 128, 64))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_post_turn_creates_turn_job(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    from sqlalchemy import select

    from call_manager.db.models import TurnJob
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/turns",
        data={"content": "Hello agent"},
    )
    assert response.status_code in (200, 201, 303)
    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.workspace_id == wsid))
        assert job is not None
        assert job.status == "queued"
        assert job.submitted_by_user_id == current_user.id


def test_post_turn_response_includes_user_turn_and_sse_connector(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    """H1 regression: response body contains both the rendered user turn
    AND the SSE-connector div (both appended to #chat-history)."""
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/turns",
        data={"content": "Hello agent"},
    )
    assert response.status_code in (200, 201)
    body = response.content.decode()
    # User turn rendered.
    assert "Hello agent" in body
    assert "chat-turn" in body
    # SSE connector div present.
    assert "sse-connect=" in body
    assert "/turns/" in body and "/stream" in body
    assert "hx-ext='sse'" in body or 'hx-ext="sse"' in body
    assert "sse-close=" in body  # closes on `done` event


def test_post_turn_escapes_html_in_user_content(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    """H2 regression: user-supplied content with HTML/script tags is
    escaped, not injected raw."""
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/turns",
        data={"content": "<script>alert('xss')</script>"},
    )
    body = response.content.decode()
    assert "<script>" not in body
    assert "&lt;script&gt;" in body


def test_post_turn_rejects_non_owner(
    client_authed_user,
    pg_session_factory,
    current_user,
):
    """Viewers cannot send turns (spec: shared-read, single-write)."""
    from call_manager.db.models import WorkspaceViewer
    from tests.proposal_assist.conftest import make_user, make_workspace

    with pg_session_factory() as s:
        owner = make_user(s, email="o@ki.se")
        ws = make_workspace(s, owner=owner)
        s.flush()
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=current_user.id))
        s.commit()
        wsid = ws.id

    response = client_authed_user.post(
        f"/workspaces/{wsid}/turns",
        data={"content": "I shouldn't be able to do this"},
    )
    assert response.status_code == 403


def test_post_turn_returns_429_on_cap_exceeded(
    client_authed_owner,
    pg_session_factory,
    current_user,
    monkeypatch,
):
    """When TokenCapExceeded is raised by submit_user_turn, the handler
    returns 429 with a useful body."""
    # Override the cap directly on the cached settings instance (env-var
    # monkeypatch doesn't take effect after Settings() is constructed).
    client_authed_owner.app.state.settings.daily_token_cap_input = 100

    from tests.proposal_assist.conftest import make_chat_turn, make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        make_chat_turn(
            s, ws, role="assistant", content={"type": "text", "text": "x"}, tokens_in=200
        )
        s.commit()
        wsid = ws.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/turns",
        data={"content": "should fail"},
    )
    assert response.status_code == 429
    assert b"cap" in response.content.lower()


# ---------------------------------------------------------------------------
# B7 Task 3: multipart turn submission (uploads + attachment_ids form field)
# ---------------------------------------------------------------------------


def test_post_turn_with_new_file_upload(
    client_authed_owner,
    pg_session_factory,
    current_user,
    tmp_path,
):
    """End-to-end multipart: a fresh image goes up alongside the text,
    persists an Attachment row, lands a chat_turns row whose content is
    a list of text+image blocks, and the response embeds the SSE
    connector + the image thumbnail."""
    client_authed_owner.app.state.settings.workspaces_dir = str(tmp_path)

    from sqlalchemy import select

    from call_manager.db.models import Attachment, ChatTurn, TurnJob
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/turns",
        data={"content": "look at this"},
        files={"files": ("fig.png", _png_bytes(), "image/png")},
    )
    assert response.status_code == 200, response.text

    with pg_session_factory() as s:
        atts = list(s.scalars(select(Attachment).where(Attachment.workspace_id == wsid)))
        turn = s.scalar(
            select(ChatTurn).where(ChatTurn.workspace_id == wsid, ChatTurn.role == "user")
        )
        job = s.scalar(select(TurnJob).where(TurnJob.workspace_id == wsid))

    assert len(atts) == 1
    aid = atts[0].id
    assert atts[0].tokens_estimate is not None
    assert atts[0].width_px == 16 and atts[0].height_px == 16
    # File written to disk by the inline-upload branch.
    assert (tmp_path / str(wsid) / "attachments" / f"{aid}.png").exists()
    # Turn content carries the image block.
    assert isinstance(turn.content, list)
    assert turn.content[0] == {"type": "text", "text": "look at this"}
    assert turn.content[1] == {"type": "image", "attachment_id": str(aid)}
    assert turn.tokens_in is not None  # pre-counted by submit_user_turn
    assert job.status == "queued"
    # Response embeds the thumbnail URL so the user sees it inline.
    assert f"/workspaces/{wsid}/files/{aid}".encode() in response.content


def test_post_turn_with_attachment_ids_form_field(
    client_authed_owner,
    pg_session_factory,
    current_user,
    tmp_path,
):
    """Pre-existing attachments (uploaded earlier via the /files panel)
    can be referenced by id on a later turn — no fresh upload required.
    """
    client_authed_owner.app.state.settings.workspaces_dir = str(tmp_path)

    from sqlalchemy import select

    from call_manager.db.models import Attachment, ChatTurn
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    # First, upload an image so it lives in the workspace's files panel.
    up = client_authed_owner.post(
        f"/workspaces/{wsid}/files",
        files={"file": ("pre.png", _png_bytes(width=20, height=20), "image/png")},
    )
    assert up.status_code == 200
    with pg_session_factory() as s:
        att = s.scalar(select(Attachment).where(Attachment.workspace_id == wsid))
        aid = att.id

    # Now submit a turn that references it by id (no fresh upload).
    response = client_authed_owner.post(
        f"/workspaces/{wsid}/turns",
        data={"content": "reuse the figure", "attachment_ids": str(aid)},
    )
    assert response.status_code == 200, response.text

    with pg_session_factory() as s:
        turn = s.scalar(
            select(ChatTurn).where(ChatTurn.workspace_id == wsid, ChatTurn.role == "user")
        )
    assert isinstance(turn.content, list)
    assert turn.content[1] == {"type": "image", "attachment_id": str(aid)}


def test_post_turn_with_cross_workspace_attachment_id_returns_400(
    client_authed_owner,
    pg_session_factory,
    current_user,
    tmp_path,
):
    """A form-supplied attachment_id from a DIFFERENT workspace must be
    rejected (400) and produce no chat_turns/turn_jobs rows."""
    client_authed_owner.app.state.settings.workspaces_dir = str(tmp_path)

    from sqlalchemy import select

    from call_manager.db.models import Attachment, ChatTurn, TurnJob
    from tests.proposal_assist.conftest import make_user, make_workspace

    with pg_session_factory() as s:
        my_ws = make_workspace(s, owner=current_user)
        other_owner = make_user(s, email="other-ws-attach@ki.se")
        other_ws = make_workspace(s, owner=other_owner)
        att = Attachment(
            workspace_id=other_ws.id,
            kind="figure",
            filename="nope.png",
            storage_path=f"{uuid.uuid4()}.png",
            mime_type="image/png",
            uploaded_by=other_owner.id,
            tokens_estimate=10,
            width_px=20,
            height_px=20,
        )
        s.add(att)
        s.commit()
        my_wsid = my_ws.id
        cross_aid = att.id

    response = client_authed_owner.post(
        f"/workspaces/{my_wsid}/turns",
        data={"content": "try the other workspace's image", "attachment_ids": str(cross_aid)},
    )
    assert response.status_code == 400
    with pg_session_factory() as s:
        turns = list(
            s.scalars(
                select(ChatTurn).where(ChatTurn.workspace_id == my_wsid, ChatTurn.role == "user")
            )
        )
        jobs = list(s.scalars(select(TurnJob).where(TurnJob.workspace_id == my_wsid)))
    assert turns == []
    assert jobs == []


def test_post_turn_with_over_5_attachments_returns_400(
    client_authed_owner,
    pg_session_factory,
    current_user,
    tmp_path,
):
    """Total cap is 5 (uploads + references combined). 6 uploads ⇒ 400
    AND every just-uploaded file is removed from disk + every row
    rolled back — no half-committed state."""
    client_authed_owner.app.state.settings.workspaces_dir = str(tmp_path)

    from sqlalchemy import select

    from call_manager.db.models import Attachment, ChatTurn
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    six = [("files", (f"{i}.png", _png_bytes(), "image/png")) for i in range(6)]
    response = client_authed_owner.post(
        f"/workspaces/{wsid}/turns",
        data={"content": "too many"},
        files=six,
    )
    assert response.status_code == 400
    with pg_session_factory() as s:
        atts = list(s.scalars(select(Attachment).where(Attachment.workspace_id == wsid)))
        turns = list(
            s.scalars(
                select(ChatTurn).where(ChatTurn.workspace_id == wsid, ChatTurn.role == "user")
            )
        )
    assert atts == [], "all uploads from the over-cap request must be rolled back"
    assert turns == []
    # No files left on disk either.
    att_dir = tmp_path / str(wsid) / "attachments"
    leftover = list(att_dir.iterdir()) if att_dir.exists() else []
    assert leftover == [], f"orphan files left on disk: {leftover}"


def test_post_turn_with_bad_mime_rolls_back_prior_upload(
    client_authed_owner,
    pg_session_factory,
    current_user,
    tmp_path,
):
    """If a SECOND file in the same multipart fails MIME validation, the
    FIRST (already-written) upload must be rolled back — no half-
    committed turn or stray orphan rows."""
    client_authed_owner.app.state.settings.workspaces_dir = str(tmp_path)

    from sqlalchemy import select

    from call_manager.db.models import Attachment, ChatTurn
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/turns",
        data={"content": "mixed bag"},
        files=[
            ("files", ("good.png", _png_bytes(), "image/png")),
            ("files", ("bad.txt", b"hello", "text/plain")),
        ],
    )
    assert response.status_code == 400
    with pg_session_factory() as s:
        atts = list(s.scalars(select(Attachment).where(Attachment.workspace_id == wsid)))
        turns = list(s.scalars(select(ChatTurn).where(ChatTurn.workspace_id == wsid)))
    assert atts == []
    assert turns == []
    att_dir = tmp_path / str(wsid) / "attachments"
    leftover = list(att_dir.iterdir()) if att_dir.exists() else []
    assert leftover == []


def test_post_turn_owner_only_viewer_403(
    client_authed_user,
    pg_session_factory,
    current_user,
):
    """POST /turns is owner-only per "shared-read, single-write" spec
    — even though viewers can stream the turn (GET /stream), they cannot
    initiate one."""
    from call_manager.db.models import WorkspaceViewer
    from tests.proposal_assist.conftest import make_user, make_workspace

    with pg_session_factory() as s:
        owner = make_user(s, email="ws-owner-b7@ki.se")
        ws = make_workspace(s, owner=owner)
        s.flush()
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=current_user.id))
        s.commit()
        wsid = ws.id

    response = client_authed_user.post(
        f"/workspaces/{wsid}/turns",
        data={"content": "viewers can't write"},
    )
    assert response.status_code == 403
