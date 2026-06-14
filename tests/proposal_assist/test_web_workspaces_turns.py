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
        ws = make_workspace(s, owner=current_user, ack_disclosure=True)
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
    """H1: response body contains both the rendered user turn AND the SSE
    connector div (both appended to #chat-history). A2: the connector uses a
    real `done` delete-child, not the no-op sse-close attribute."""
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=True)
        s.commit()
        wsid = ws.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/turns",
        data={"content": "Hello agent"},
    )
    assert response.status_code in (200, 201)
    body = response.content.decode()
    assert "Hello agent" in body
    assert "chat-turn" in body
    assert "sse-connect=" in body
    assert "/turns/" in body and "/stream" in body
    assert 'hx-ext="sse"' in body
    # A2: real cleanup via a done delete-child; the no-op sse-close is gone.
    assert 'sse-swap="done"' in body
    assert 'hx-swap="delete"' in body
    assert "sse-close=" not in body


def test_post_turn_echo_carries_stable_chat_turn_id(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    """A2: the echo renders the PERSISTED user turn (real id), so the client
    dedup can collapse it against the same row arriving via another live
    connector."""
    from sqlalchemy import select

    from call_manager.db.models import ChatTurn, TurnJob
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=True)
        s.commit()
        wsid = ws.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/turns",
        data={"content": "Hello agent"},
    )
    assert response.status_code in (200, 201)
    body = response.content.decode()

    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.workspace_id == wsid))
        user_turn_id = job.user_turn_id
        # user_turn_id is a real persisted row (nullable FK, but set here).
        assert s.get(ChatTurn, user_turn_id) is not None

    assert f'id="chat-turn-{user_turn_id}"' in body


def test_post_turn_connector_shows_cancel_for_owner(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    """The owner who submits a queued turn gets a Cancel control on the
    connector, targeting its own #cancel-status span (not #chat-history)."""
    from sqlalchemy import select

    from call_manager.db.models import TurnJob
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=True)
        s.commit()
        wsid = ws.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/turns",
        data={"content": "Hello agent"},
    )
    body = response.content.decode()
    with pg_session_factory() as s:
        jid = s.scalar(select(TurnJob.id).where(TurnJob.workspace_id == wsid))
    assert f'id="cancel-status-{jid}"' in body
    assert f"/turns/{jid}/cancel" in body


def test_post_turn_escapes_html_in_user_content(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    """H2 regression: user-supplied content with HTML/script tags is
    escaped, not injected raw."""
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=True)
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
        ws = make_workspace(s, owner=current_user, ack_disclosure=True)
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
        ws = make_workspace(s, owner=current_user, ack_disclosure=True)
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
        ws = make_workspace(s, owner=current_user, ack_disclosure=True)
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
        my_ws = make_workspace(s, owner=current_user, ack_disclosure=True)
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
        ws = make_workspace(s, owner=current_user, ack_disclosure=True)
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
        ws = make_workspace(s, owner=current_user, ack_disclosure=True)
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


# ---------------------------------------------------------------------------
# B7 Task 4: chat history partial renders text + image blocks uniformly.
#
# These tests pin the partial directly (rendering through Jinja) so the
# three places that emit chat-turn HTML — the history loop, the SSE
# renderer, and the post_turn echo — share one well-tested implementation.
# ---------------------------------------------------------------------------


def _render_partial(turn) -> str:
    """Render partials/chat_turn.html for ``turn`` and return the HTML."""
    from call_manager.proposal_assist.web.workspaces import (
        _CHAT_TURN_PARTIAL,
        _default_templates,
    )

    return _default_templates().env.get_template(_CHAT_TURN_PARTIAL).render(turn=turn)


def test_chat_turn_partial_renders_plain_string_content():
    """Legacy/pre-B7: ChatTurn.content == str (single-string body)."""
    from call_manager.db.models import ChatTurn

    turn = ChatTurn(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        role="user",
        content="plain text",  # type: ignore[arg-type]
        position=1,
    )
    html = _render_partial(turn)
    assert "<p>plain text</p>" in html
    assert 'data-role="user"' in html


def test_chat_turn_partial_renders_single_text_dict_content():
    """Pre-B7: ChatTurn.content == {"type":"text","text":"..."}."""
    from call_manager.db.models import ChatTurn

    turn = ChatTurn(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        role="assistant",
        content={"type": "text", "text": "single dict"},
        position=1,
    )
    html = _render_partial(turn)
    assert "<p>single dict</p>" in html


def test_chat_turn_partial_renders_list_of_text_and_image_blocks():
    """B7: list content of text + image blocks renders <p> + <img>."""
    from call_manager.db.models import ChatTurn

    wsid = uuid.uuid4()
    aid = uuid.uuid4()
    turn = ChatTurn(
        id=uuid.uuid4(),
        workspace_id=wsid,
        role="user",
        content=[  # type: ignore[arg-type]
            {"type": "text", "text": "hello"},
            {"type": "image", "attachment_id": str(aid)},
        ],
        position=1,
    )
    html = _render_partial(turn)
    assert "<p>hello</p>" in html
    expected_src = f"/workspaces/{wsid}/files/{aid}"
    assert expected_src in html
    assert "<img" in html
    assert 'class="chat-image"' in html


def test_chat_turn_partial_renders_image_only_list_block():
    """B7: list content with a single image block (no text) still renders
    the <img> thumbnail."""
    from call_manager.db.models import ChatTurn

    wsid = uuid.uuid4()
    aid = uuid.uuid4()
    turn = ChatTurn(
        id=uuid.uuid4(),
        workspace_id=wsid,
        role="user",
        content=[{"type": "image", "attachment_id": str(aid)}],  # type: ignore[arg-type]
        position=1,
    )
    html = _render_partial(turn)
    assert f"/workspaces/{wsid}/files/{aid}" in html
    assert "<img" in html
    # No stray <p> when text is absent.
    assert "<p>" not in html


def test_chat_turn_partial_escapes_text_in_list_blocks():
    """B7 XSS regression: HTML in a list-of-blocks text block is escaped
    (defense in depth — the partial's auto-escape must apply to nested
    block content, not just the top-level dict shape)."""
    from call_manager.db.models import ChatTurn

    turn = ChatTurn(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        role="user",
        content=[{"type": "text", "text": "<script>alert(1)</script>"}],  # type: ignore[arg-type]
        position=1,
    )
    html = _render_partial(turn)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_chat_history_view_renders_image_thumbnail(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    """Full-page render: a stored ChatTurn whose content is a list with
    an image block surfaces an <img src="/workspaces/<wsid>/files/<aid>">
    in the main view's chat history."""
    from tests.proposal_assist.conftest import make_chat_turn, make_workspace

    aid = uuid.uuid4()
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=True)
        make_chat_turn(
            s,
            ws,
            role="user",
            content=[
                {"type": "text", "text": "look at this fig"},
                {"type": "image", "attachment_id": str(aid)},
            ],  # type: ignore[arg-type]
        )
        s.commit()
        wsid = ws.id

    response = client_authed_owner.get(f"/workspaces/{wsid}")
    assert response.status_code == 200
    body = response.content.decode()
    assert "<p>look at this fig</p>" in body
    assert f"/workspaces/{wsid}/files/{aid}" in body
    assert "chat-image" in body


def test_post_turn_error_returns_json_detail(monkeypatch, pg_url, pg_session_factory, tmp_path):
    """Reflected-XSS regression (B1/Task 12): a malicious Content-Type on
    an upload failure is surfaced as a JSON ``{"detail": ...}`` body, not
    an HTML fragment. The rejection used to render server-escaped HTML;
    now post_turn raises HTTPException and app.js renders ``detail`` via
    ``element.textContent`` (XSS-safe by construction). The on-the-wire
    body is therefore JSON whose ``detail`` is plain text — no HTML
    structure, and the response content-type is application/json, so the
    malicious markup can never be parsed as a live ``<script>`` element
    when swapped into the page."""
    import io

    from fastapi.testclient import TestClient

    from call_manager.auth.session_cookie import SessionSigner
    from call_manager.db.models import User, Workspace
    from call_manager.main import create_app

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("WORKSPACES_DIR", str(tmp_path))
    app = create_app()

    with pg_session_factory() as s:
        owner = User(status="active", email="owner@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="Test ws", current_phase="spec")
        s.add(ws)
        s.commit()
        uid, wsid = owner.id, ws.id

    client = TestClient(app, raise_server_exceptions=True)
    client.__enter__()
    try:
        signer: SessionSigner = app.state.session_signer
        client.cookies.set("session", signer.make(user_id=uid))
        client.post(f"/workspaces/{wsid}/disclosure")  # clear server-side disclosure gate
        attack = "image/png<script>alert(1)</script>"
        resp = client.post(
            f"/workspaces/{wsid}/turns",
            data={"content": "hi"},
            files={"files": ("x.png", io.BytesIO(b"x"), attack)},
        )
        assert resp.status_code == 400
        # Body is JSON (not an HTML fragment): the rejection is rendered
        # client-side via textContent, so the malicious markup is confined
        # to a JSON string value rather than landing as live HTML.
        assert resp.headers["content-type"].startswith("application/json")
        detail = resp.json()["detail"]
        # The attacker-controlled content_type is echoed verbatim in the
        # plain-text detail (no longer HTML-escaped — escaping happened to
        # protect an HTML body that no longer exists).
        assert "Unsupported content_type" in detail
        assert attack in detail
    finally:
        client.__exit__(None, None, None)


def test_post_turn_with_reference_ids_form_field(monkeypatch, pg_url, pg_session_factory, tmp_path):
    """End-to-end: POST a turn with reference_ids; the ChatTurn row's
    content includes a reference block; tokens_in pre-count includes
    the reference chars."""
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from call_manager.auth.session_cookie import SessionSigner
    from call_manager.db.models import ChatTurn, Reference, User, Workspace
    from call_manager.main import create_app

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("WORKSPACES_DIR", str(tmp_path))

    app = create_app()
    with pg_session_factory() as s:
        owner = User(status="active", email=f"o-{uuid.uuid4()}@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="T", current_phase="spec")
        s.add(ws)
        s.flush()
        ref = Reference(
            workspace_id=ws.id,
            kind="paper",
            title="My Paper",
            filename="my.pdf",
            storage_path=f"references/{uuid.uuid4()}/original.pdf",
            mime_type="application/pdf",
            source_type="uploaded",
            uploaded_by=None,
            text_status="done",
            text_chars_total=80,
        )
        s.add(ref)
        s.commit()
        uid, wsid, refid = owner.id, ws.id, ref.id

    client = TestClient(app, raise_server_exceptions=True)
    client.__enter__()
    signer: SessionSigner = app.state.session_signer
    client.cookies.set("session", signer.make(user_id=uid))
    client.post(f"/workspaces/{wsid}/disclosure")  # clear server-side disclosure gate
    try:
        resp = client.post(
            f"/workspaces/{wsid}/turns",
            data={
                "content": "talk about this",
                "reference_ids": str(refid),
            },
        )
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200, resp.text

    with pg_session_factory() as s:
        ct = s.scalar(
            select(ChatTurn).where(ChatTurn.workspace_id == wsid, ChatTurn.role == "user")
        )
    assert isinstance(ct.content, list)
    assert any(
        b.get("type") == "reference" and b.get("reference_id") == str(refid) for b in ct.content
    )
    # Pre-count: 80 // 4 + len("talk about this") // 4 = 20 + 3 = 23
    assert ct.tokens_in == 20 + (len("talk about this") // 4)


def test_post_turn_with_reference_ids_only_echoes_reference_chip(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """Slice 4 residuals regression: a refs-only turn (no fresh upload,
    no attachment_ids) must echo the reference chip in the immediate
    response body — not a text-only render that drops the reference
    until the SSE re-render lands.

    Asserts:
      * 200 status with a refs-only request.
      * Response body contains the reference's title (chip rendered).
      * Response body does NOT contain raw JSON like ``{'type': 'reference``
        — proves the block rendered as HTML, not as a repr.
    """
    from fastapi.testclient import TestClient

    from call_manager.auth.session_cookie import SessionSigner
    from call_manager.db.models import Reference, User, Workspace
    from call_manager.main import create_app

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("WORKSPACES_DIR", str(tmp_path))

    app = create_app()
    with pg_session_factory() as s:
        owner = User(status="active", email=f"o-{uuid.uuid4()}@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="T", current_phase="spec")
        s.add(ws)
        s.flush()
        ref = Reference(
            workspace_id=ws.id,
            kind="paper",
            title="Echo Chip Paper",
            filename="echo.pdf",
            storage_path=f"references/{uuid.uuid4()}/original.pdf",
            mime_type="application/pdf",
            source_type="uploaded",
            uploaded_by=None,
            text_status="done",
            text_chars_total=40,
        )
        s.add(ref)
        s.commit()
        uid, wsid, refid = owner.id, ws.id, ref.id

    client = TestClient(app, raise_server_exceptions=True)
    client.__enter__()
    signer: SessionSigner = app.state.session_signer
    client.cookies.set("session", signer.make(user_id=uid))
    client.post(f"/workspaces/{wsid}/disclosure")  # clear server-side disclosure gate
    try:
        resp = client.post(
            f"/workspaces/{wsid}/turns",
            data={
                "content": "look at this paper",
                "reference_ids": str(refid),
            },
        )
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200, resp.text
    body = resp.content.decode()
    # Chip rendered with the reference's picker label (title).
    assert "Echo Chip Paper" in body
    # And as the HTML chip wrapper, not raw JSON.
    assert "ref-chip" in body
    assert "{'type': 'reference" not in body
    assert '{"type": "reference' not in body


def test_post_turn_with_reference_ids_returns_400_for_pending_reference(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """text_status='pending' → submit raises ValueError → route returns 400."""
    from fastapi.testclient import TestClient

    from call_manager.auth.session_cookie import SessionSigner
    from call_manager.db.models import Reference, User, Workspace
    from call_manager.main import create_app

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("WORKSPACES_DIR", str(tmp_path))

    app = create_app()
    with pg_session_factory() as s:
        owner = User(status="active", email=f"o-{uuid.uuid4()}@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="T", current_phase="spec")
        s.add(ws)
        s.flush()
        ref = Reference(
            workspace_id=ws.id,
            kind="paper",
            title="X",
            filename="x.pdf",
            storage_path=f"references/{uuid.uuid4()}/original.pdf",
            mime_type="application/pdf",
            source_type="uploaded",
            uploaded_by=None,
            text_status="pending",
            text_chars_total=None,
        )
        s.add(ref)
        s.commit()
        uid, wsid, refid = owner.id, ws.id, ref.id

    client = TestClient(app, raise_server_exceptions=True)
    client.__enter__()
    signer: SessionSigner = app.state.session_signer
    client.cookies.set("session", signer.make(user_id=uid))
    client.post(f"/workspaces/{wsid}/disclosure")  # clear server-side disclosure gate
    try:
        resp = client.post(
            f"/workspaces/{wsid}/turns",
            data={"content": "x", "reference_ids": str(refid)},
        )
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 400
    assert b"not ready" in resp.content


def test_workspace_main_context_includes_workspace_references(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """The composer picker needs the list of done-references in the
    template context. Only text_status='done' refs appear (pending /
    failed are filtered out)."""
    from fastapi.testclient import TestClient

    from call_manager.auth.session_cookie import SessionSigner
    from call_manager.db.models import Reference, User, Workspace
    from call_manager.main import create_app

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("WORKSPACES_DIR", str(tmp_path))

    app = create_app()
    with pg_session_factory() as s:
        owner = User(status="active", email=f"o-{uuid.uuid4()}@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="T", current_phase="spec")
        s.add(ws)
        s.flush()
        # Two refs: one done (should appear in picker), one pending (filtered out)
        ref_done = Reference(
            workspace_id=ws.id,
            kind="paper",
            title="Done",
            filename="d.pdf",
            storage_path=f"references/{uuid.uuid4()}/original.pdf",
            mime_type="application/pdf",
            source_type="uploaded",
            uploaded_by=None,
            text_status="done",
            text_chars_total=10,
        )
        ref_pending = Reference(
            workspace_id=ws.id,
            kind="paper",
            title="Pending",
            filename="p.pdf",
            storage_path=f"references/{uuid.uuid4()}/original.pdf",
            mime_type="application/pdf",
            source_type="uploaded",
            uploaded_by=None,
            text_status="pending",
            text_chars_total=None,
        )
        s.add_all([ref_done, ref_pending])
        s.commit()
        uid, wsid = owner.id, ws.id
        done_id = ref_done.id
        pending_id = ref_pending.id

    client = TestClient(app, raise_server_exceptions=True)
    client.__enter__()
    signer: SessionSigner = app.state.session_signer
    client.cookies.set("session", signer.make(user_id=uid))
    client.post(f"/workspaces/{wsid}/disclosure")  # clear server-side disclosure gate
    try:
        resp = client.get(f"/workspaces/{wsid}")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 200
    body = resp.content.decode()
    # Composer picker should list the done ref but not the pending one
    assert "Done" in body
    assert 'name="reference_ids"' in body
    # The done ref's checkbox value should be present.
    assert f'value="{done_id}"' in body
    # The pending ref's id should NOT appear as a picker value (filtered server-side).
    assert f'value="{pending_id}"' not in body


def test_reload_renders_connector_for_active_running_job(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    """Mid-turn reload: get_workspace_main renders a connector for the active
    job, seeded at the last rendered chat position. A running job shows the
    working indicator but no Cancel."""
    from datetime import UTC, datetime

    from sqlalchemy import select

    from call_manager.db.models import ChatTurn, TurnJob
    from tests.proposal_assist.conftest import make_chat_turn, make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=True)
        ut = ChatTurn(workspace_id=ws.id, role="user", content={"type": "text", "text": "hi"})
        s.add(ut)
        s.flush()
        job = TurnJob(
            workspace_id=ws.id,
            user_turn_id=ut.id,
            submitted_by_user_id=current_user.id,
            status="running",
            queued_at=datetime.now(UTC),
            started_at=datetime.now(UTC),
        )
        s.add(job)
        make_chat_turn(s, ws, role="assistant", content={"type": "text", "text": "partial"})
        s.commit()
        wsid, jid = ws.id, job.id
        max_pos = s.scalar(
            select(ChatTurn.position)
            .where(ChatTurn.workspace_id == wsid)
            .order_by(ChatTurn.position.desc())
        )

    body = client_authed_owner.get(f"/workspaces/{wsid}").content.decode()
    assert f'id="sse-mount-{jid}"' in body
    # The connector seed reflects the FULL rendered history (necessary, not
    # sufficient, for the ordering guarantee — see the note below the test).
    assert f"?since={max_pos}" in body
    assert "Working" in body
    # Running job: no Cancel control.
    assert f"/turns/{jid}/cancel" not in body


def test_reload_connector_for_queued_job_shows_cancel(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    from datetime import UTC, datetime

    from call_manager.db.models import ChatTurn, TurnJob
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=True)
        ut = ChatTurn(workspace_id=ws.id, role="user", content={"type": "text", "text": "hi"})
        s.add(ut)
        s.flush()
        job = TurnJob(
            workspace_id=ws.id,
            user_turn_id=ut.id,
            submitted_by_user_id=current_user.id,
            status="queued",
            queued_at=datetime.now(UTC),
        )
        s.add(job)
        s.commit()
        wsid, jid = ws.id, job.id

    body = client_authed_owner.get(f"/workspaces/{wsid}").content.decode()
    assert f'id="sse-mount-{jid}"' in body
    assert f"/turns/{jid}/cancel" in body


def test_reload_no_connector_when_no_active_job(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=True)
        s.commit()
        wsid = ws.id

    body = client_authed_owner.get(f"/workspaces/{wsid}").content.decode()
    assert "sse-mount-" not in body
    assert "data-workspace-id" in body


def test_reload_connector_no_cancel_for_viewer(
    client_authed_user,
    pg_session_factory,
    current_user,
):
    """A viewer may watch the connector but never gets a Cancel control."""
    from datetime import UTC, datetime

    from call_manager.db.models import ChatTurn, TurnJob, WorkspaceViewer
    from tests.proposal_assist.conftest import make_user, make_workspace

    with pg_session_factory() as s:
        owner = make_user(s, email="owner2@ki.se")
        ws = make_workspace(s, owner=owner, ack_disclosure=True)
        ut = ChatTurn(workspace_id=ws.id, role="user", content={"type": "text", "text": "hi"})
        s.add(ut)
        s.flush()
        job = TurnJob(
            workspace_id=ws.id,
            user_turn_id=ut.id,
            submitted_by_user_id=owner.id,
            status="queued",
            queued_at=datetime.now(UTC),
        )
        s.add(job)
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=current_user.id))
        s.commit()
        wsid, jid = ws.id, job.id

    body = client_authed_user.get(f"/workspaces/{wsid}").content.decode()
    assert f'id="sse-mount-{jid}"' in body  # viewer sees the connector
    assert f"/turns/{jid}/cancel" not in body  # but no Cancel


def test_history_loop_renders_stable_chat_turn_id(
    client_authed_owner, pg_session_factory, current_user
):
    """The third dedup render path: the full-page history loop must emit the
    same id="chat-turn-{id}" the echo + SSE paths do, so client dedup can match
    a row across all three. Completes the three-path stable-id contract."""
    from call_manager.db.models import ChatTurn
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=True)
        turn = ChatTurn(
            workspace_id=ws.id, role="user", content={"type": "text", "text": "history row"}
        )
        s.add(turn)
        s.commit()
        wsid, tid = ws.id, turn.id

    body = client_authed_owner.get(f"/workspaces/{wsid}").content.decode()
    assert f'id="chat-turn-{tid}"' in body
