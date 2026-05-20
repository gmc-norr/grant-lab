"""XSS regression tests for B6 (Codex rev2 H2).

Two defenses must hold:
  1. proposal.md content rendered to HTML goes through bleach.clean,
     stripping <script>, <iframe>, on*= handlers, etc.
  2. chat_turns content rendered to HTML via Jinja's default
     auto-escape — no `| safe` anywhere on chat content.

These tests render full pages (via the test client) and assert that
known-dangerous content does NOT appear unescaped in the response."""

import subprocess
from pathlib import Path

from tests.proposal_assist.conftest import make_chat_turn, make_workspace


def _init_repo_with_proposal(workspaces_dir: Path, wsid, content: str) -> None:
    """Create a one-commit git repo whose proposal.md is `content`."""
    repo_dir = workspaces_dir / str(wsid) / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "a@b"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo_dir, check=True)
    (repo_dir / "proposal.md").write_text(content)
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_dir, check=True)


def test_proposal_md_with_script_tag_is_sanitized(
    client_authed_owner,
    pg_session_factory,
    current_user,
    tmp_path,
):
    """proposal.md containing <script>alert('xss')</script> renders
    WITHOUT executable script tags (bleach strips them with strip=True).

    Note: bleach strip=True removes the tag pair but keeps the inner
    text content as plain text (so 'alert' text will appear — but it
    is NOT executable without the enclosing <script> tag). The defenses
    proven here: no raw <script> open-tag in the output."""
    client_authed_owner.app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id
    _init_repo_with_proposal(
        tmp_path,
        wsid,
        "# Heading\n\n<script>alert('xss')</script>\n\nBody.",
    )

    response = client_authed_owner.get(f"/workspaces/{wsid}")
    assert response.status_code == 200
    body = response.content
    # The script tag MUST NOT appear (bleach strips the tag; only static
    # htmx <script src=...> includes exist — not injection from proposal.md).
    # We check for the injection marker (closing tag) since htmx includes
    # use opening <script src=...> tags but no raw </script> in content.
    # Most critically: no executable <script> wrapping agent-controlled text.
    # Check the artifact section only — the page head has htmx <script> tags.
    artifact_section_start = body.find(b'id="artifact-rendered"')
    assert artifact_section_start != -1, "artifact section not found"
    artifact_section = body[artifact_section_start:]
    assert b"<script>" not in artifact_section
    assert b"</script>" not in artifact_section
    # The legit content around it should still render.
    assert b"Heading" in body
    assert b"Body" in body


def test_proposal_md_with_event_handler_attr_is_sanitized(
    client_authed_owner,
    pg_session_factory,
    current_user,
    tmp_path,
):
    """An <img onerror=...> in proposal.md is stripped (img isn't in
    ALLOWED_TAGS for proposal markdown rendering)."""
    client_authed_owner.app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id
    _init_repo_with_proposal(
        tmp_path,
        wsid,
        "<img src=x onerror=\"alert('xss')\">",
    )

    response = client_authed_owner.get(f"/workspaces/{wsid}")
    assert response.status_code == 200
    body = response.content
    assert b"<img" not in body
    assert b"onerror" not in body
    assert b"alert" not in body


def test_chat_turn_content_with_html_is_escaped(
    client_authed_owner,
    pg_session_factory,
    current_user,
    tmp_path,
):
    """chat_turns content like '<img src=x onerror=alert(1)>' renders
    escaped in the main view (Jinja auto-escape, NO `| safe` on chat
    content).

    Jinja escapes '<' and '>' to '&lt;'/'&gt;', rendering the injection
    non-executable. The attribute string 'onerror=alert(1)' is visible as
    text (Jinja only escapes angle brackets + quotes, not attr names), but
    it cannot execute because the surrounding '<img' is escaped to '&lt;img'.
    """
    client_authed_owner.app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        make_chat_turn(
            s,
            ws,
            role="assistant",
            content={"type": "text", "text": "<img src=x onerror=alert(1)>"},
        )
        s.commit()
        wsid = ws.id

    response = client_authed_owner.get(f"/workspaces/{wsid}")
    assert response.status_code == 200
    body = response.content
    # Raw <img> tag must NOT appear — angle brackets are escaped by Jinja.
    assert b"<img" not in body
    # The escaped opening form SHOULD appear: proof of escape-not-omit.
    assert b"&lt;img" in body


def test_sse_helper_escapes_script_content():
    """Direct unit test on _render_chat_turn_html — defense in depth
    even when invoked outside the route (e.g., future call sites)."""
    import uuid

    from call_manager.db.models import ChatTurn
    from call_manager.proposal_assist.web.workspaces import (
        _render_chat_turn_html,
    )

    turn = ChatTurn(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        role="assistant",
        content={"type": "text", "text": "<script>alert('x')</script>"},
        position=1,
    )
    snippet = _render_chat_turn_html(turn)
    assert "<script>" not in snippet
    assert "&lt;script&gt;" in snippet
