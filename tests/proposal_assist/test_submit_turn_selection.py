"""POST /api/workspaces/{id}/turns — selection_context field wiring (Task 2).

Reuses the _app / _seed / _client / _init_repo helpers from test_api_workspaces /
test_editing_save (same session-factory idiom: app binds from DATABASE_URL, and
pg_session_factory rides the same engine).

`active_ws_client` seeds a workspace with a committed proposal.md (two H2
sections so section_index=1 is stable), acks disclosure for the owner, and
returns (owner_client, ws_id, head_sha, section_dict).

`member_of(ws_id)` returns a TestClient authed as the pre-seeded WorkspaceViewer.
`stranger` is authed as the pre-seeded stranger user (not a member).
"""

from __future__ import annotations

import pytest

from grant_lab.db.models import WorkspaceEvent
from grant_lab.proposal_assist.citations.gitio import current_head
from grant_lab.proposal_assist.editing.sectionizer import split_sections
from grant_lab.proposal_assist.paths import workspace_repo_dir
from tests.test_api_workspaces import _app, _client, _seed

# Proposal with a preamble (index 0) and two H2 sections (index 1, 2).
_DOC = "# Title\npreamble\n## Background\nold background body\n## Aims\naims text\n"


def _ack_disclosure(pg_session_factory, ws_id, user_id):
    with pg_session_factory() as s:
        s.add(WorkspaceEvent(workspace_id=ws_id, kind="disclosure_acknowledged", actor_id=user_id))
        s.commit()


@pytest.fixture
def _seeded(make_app, monkeypatch, pg_url, pg_session_factory, tmp_path):
    """One app + seeded workspace (owner + member + stranger + git repo)."""
    app = _app(make_app, monkeypatch, pg_url, tmp_path)
    owner_id, member_id, stranger_id, ws_id = _seed(app, pg_session_factory, tmp_path, _DOC)
    repo = workspace_repo_dir(tmp_path, ws_id)
    head = current_head(repo)
    sections = split_sections(_DOC)
    # section index 1 = "Background". NOTE: sec.text INCLUDES the "## Background\n"
    # heading line (the sha is over the whole section text), so anchor=section["text"]
    # with to_offset=len(section["text"]) selects the entire section incl. its heading.
    sec = sections[1]
    section = {"index": 1, "sha": sec.sha, "text": sec.text, "heading": sec.heading}
    return app, owner_id, member_id, stranger_id, ws_id, head, section


@pytest.fixture
def active_ws_client(_seeded, pg_session_factory):
    """(owner_client, ws_id, head, section) — disclosure pre-acked for owner."""
    app, owner_id, member_id, stranger_id, ws_id, head, section = _seeded
    _ack_disclosure(pg_session_factory, ws_id, owner_id)
    client = _client(app, owner_id)
    return client, ws_id, head, section


@pytest.fixture
def member_of(_seeded, pg_session_factory):
    """Returns a factory: member_of(ws_id) → TestClient authed as the pre-seeded member.
    The member's disclosure is pre-acked so selection-ask passes the gate."""
    app, owner_id, member_id, stranger_id, ws_id, head, section = _seeded

    def _factory(ws_id_arg):
        # Ack disclosure for the member so the gate_cloud_llm disclosure check passes.
        _ack_disclosure(pg_session_factory, ws_id_arg, member_id)
        return _client(app, member_id)

    return _factory


@pytest.fixture
def stranger(_seeded):
    """TestClient authed as the stranger (not a workspace member)."""
    app, owner_id, member_id, stranger_id, ws_id, head, section = _seeded
    return _client(app, stranger_id)


# ---------------------------------------------------------------------------
# Core selection-context behaviors (owner)
# ---------------------------------------------------------------------------


def test_modify_on_clean_section_stores_selection_block(active_ws_client):
    client, ws, head, section = active_ws_client
    frm, to = 0, len(section["text"])
    r = client.post(
        f"/api/workspaces/{ws}/turns",
        json={
            "content": "Shorten this.",
            "selection_context": {
                "request_type": "modify",
                "section_index": section["index"],
                "base_commit_sha": head,
                "base_section_sha": section["sha"],
                "base_branch": "main",
                "anchor": section["text"],
                "from_offset": frm,
                "to_offset": to,
            },
        },
    )
    assert r.status_code == 200
    # The stored user turn carries a selection block built by the server.
    turn = r.json()["user_turn"]
    blocks = turn["content"] if isinstance(turn["content"], list) else [turn["content"]]
    sel = [b for b in blocks if b.get("type") == "selection"]
    assert sel and sel[0]["verified"] is True and sel[0]["request_type"] == "modify"


def test_modify_on_dirty_section_returns_409(active_ws_client):
    client, ws, head, section = active_ws_client
    r = client.post(
        f"/api/workspaces/{ws}/turns",
        json={
            "content": "Shorten this.",
            "selection_context": {
                "request_type": "modify",
                "section_index": section["index"],
                "base_commit_sha": head,
                "base_section_sha": "stale" * 8,
                "base_branch": "main",
                "anchor": "whatever",
                "from_offset": 0,
                "to_offset": 8,
            },
        },
    )
    assert r.status_code == 409


def test_ask_on_dirty_section_allowed_unverified(active_ws_client):
    client, ws, head, section = active_ws_client
    r = client.post(
        f"/api/workspaces/{ws}/turns",
        json={
            "content": "Explain this.",
            "selection_context": {
                "request_type": "ask",
                "section_index": section["index"],
                "base_commit_sha": head,
                "base_section_sha": "stale" * 8,
                "base_branch": "main",
                "anchor": "my unsaved words",
                "from_offset": 0,
                "to_offset": 16,
            },
        },
    )
    assert r.status_code == 200
    turn = r.json()["user_turn"]
    blocks = turn["content"] if isinstance(turn["content"], list) else [turn["content"]]
    sel = [b for b in blocks if b.get("type") == "selection"][0]
    assert sel["verified"] is False


# ---------------------------------------------------------------------------
# viewer-Ask authorization (spec §7)
# ---------------------------------------------------------------------------


def test_member_can_submit_selection_ask(active_ws_client, member_of):
    client, ws, head, section = active_ws_client
    mclient = member_of(ws)
    r = mclient.post(
        f"/api/workspaces/{ws}/turns",
        json={
            "content": "Explain this.",
            "selection_context": {
                "request_type": "ask",
                "section_index": section["index"],
                "base_commit_sha": head,
                "base_section_sha": section["sha"],
                "base_branch": "main",
                "anchor": section["text"],
                "from_offset": 0,
                "to_offset": len(section["text"]),
            },
        },
    )
    assert r.status_code == 200


def test_member_cannot_submit_modify(active_ws_client, member_of):
    client, ws, head, section = active_ws_client
    mclient = member_of(ws)
    r = mclient.post(
        f"/api/workspaces/{ws}/turns",
        json={
            "content": "Shorten.",
            "selection_context": {
                "request_type": "modify",
                "section_index": section["index"],
                "base_commit_sha": head,
                "base_section_sha": section["sha"],
                "base_branch": "main",
                "anchor": section["text"],
                "from_offset": 0,
                "to_offset": len(section["text"]),
            },
        },
    )
    assert r.status_code == 403  # owner only — members may not modify


def test_member_cannot_free_chat(active_ws_client, member_of):
    client, ws, head, section = active_ws_client
    mclient = member_of(ws)
    r = mclient.post(f"/api/workspaces/{ws}/turns", json={"content": "hi"})
    assert r.status_code == 403  # no selection -> owner-only free chat


def test_non_member_ask_forbidden(active_ws_client, stranger):
    client, ws, head, section = active_ws_client
    r = stranger.post(
        f"/api/workspaces/{ws}/turns",
        json={
            "content": "Explain.",
            "selection_context": {
                "request_type": "ask",
                "section_index": section["index"],
                "base_commit_sha": head,
                "base_section_sha": section["sha"],
                "base_branch": "main",
                "anchor": section["text"],
                "from_offset": 0,
                "to_offset": len(section["text"]),
            },
        },
    )
    assert r.status_code in (403, 404)  # not a workspace member
