"""POST /api/workspaces/{id}/turns — collab Edit submit relocates against the LIVE
room (3b-5d.D1b).

A unit test on build_selection_block cannot catch a plan that adds
allow_anchor_relocation but forgets to pass it from the collab-draft branch of
turns.py (Codex-Medium); and a same-content stub cannot prove the baseline is the
LIVE room rather than git. This drives the real route with the live Yjs room
genuinely AHEAD of git HEAD: git has NO "diagnostic", the live log DOES. A
git-baselined relocate would 409 (anchor gone); the live baseline returns 200 with
the offset recomputed from the live section.
"""

from __future__ import annotations

from grant_lab.db.models import WorkspaceEvent
from grant_lab.proposal_assist.collab import state, store
from grant_lab.proposal_assist.collab.canonical import canonical_artifact_markdown
from grant_lab.proposal_assist.collab.hydrate import build_doc, load_or_hydrate
from grant_lab.proposal_assist.collab.markdown_bridge import markdown_to_pm
from grant_lab.proposal_assist.collab.yjs_bridge import apply_pm_section_to_fragment
from grant_lab.proposal_assist.editing.sectionizer import split_sections
from grant_lab.proposal_assist.paths import workspace_repo_dir
from tests.proposal_assist.conftest import stamp_for
from tests.test_api_workspaces import _app, _client, _seed

# git: preamble (0) + Aims (1) WITHOUT "diagnostic". The live room adds it.
_DOC_GIT = "# Title\npreamble\n## Aims\nPrecision medicine outcomes.\n"
_LIVE_AIMS = "## Aims\nPrecision medicine improves diagnostic outcomes.\n"


def _ack(pg_session_factory, ws_id, user_id):
    with pg_session_factory() as s:
        s.add(WorkspaceEvent(workspace_id=ws_id, kind="disclosure_acknowledged", actor_id=user_id))
        s.commit()


def test_collab_modify_relocates_against_live_room(
    make_app, monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _app(make_app, monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, _DOC_GIT)
    # Enable collab, hydrate the room from git (log = _DOC_GIT), then apply a live edit
    # to section 1 that introduces "diagnostic" → the room is now AHEAD of git HEAD.
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=ws_id, artifact="draft")
        updates = load_or_hydrate(
            s, stamp=stamp_for(s, ws_id), workspace_id=ws_id, artifact="draft",
            workspaces_dir=str(tmp_path)
        )
        room_doc = build_doc(updates)
        blocks = markdown_to_pm(_LIVE_AIMS).get("content") or []
        update = apply_pm_section_to_fragment(room_doc, section_index=1, blocks=blocks)
        store.append_update(
            s, workspace_id=ws_id, artifact="draft", actor_id=owner_id, update=update, commit=False
        )
        s.commit()
    _ack(pg_session_factory, ws_id, owner_id)

    # The offset the backend will compute — canonical LIVE read → split → index.
    with pg_session_factory() as s:
        live_md = canonical_artifact_markdown(
            s, workspace_id=ws_id, artifact="draft", repo_dir=workspace_repo_dir(tmp_path, ws_id)
        )
    expected_off = split_sections(live_md)[1].text.index("diagnostic")

    client = _client(app, owner_id)
    r = client.post(
        f"/api/workspaces/{ws_id}/turns",
        json={
            "content": "Shorten this.",
            "selection_context": {
                "request_type": "modify",
                "section_index": 1,
                "base_commit_sha": "0" * 40,
                "base_section_sha": "stale" * 8,  # git sha the room is ahead of
                "base_branch": "main",
                "anchor": "diagnostic",           # present in LIVE, absent from git HEAD
                "from_offset": 999,               # bogus client offsets — must be recomputed
                "to_offset": 1009,
            },
        },
    )
    assert r.status_code == 200  # a git-baselined relocate would 409 (not in git HEAD)
    turn = r.json()["user_turn"]
    blocks_out = turn["content"] if isinstance(turn["content"], list) else [turn["content"]]
    sel = [b for b in blocks_out if b.get("type") == "selection"][0]
    assert sel["verified"] is True and sel["request_type"] == "modify"
    assert sel["anchor"] == "diagnostic"
    assert sel["from_offset"] == expected_off  # the LIVE section offset, not git, not 999
    assert sel["to_offset"] == expected_off + len("diagnostic")
