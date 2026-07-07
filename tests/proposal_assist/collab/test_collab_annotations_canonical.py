"""3b-5d.D4 §3.4 (Codex Medium): the Claims tab sectionizes the LIVE room, not
stale git, once draft collab is enabled."""

import pytest

from call_manager.db.models import UserClaimAnnotation
from call_manager.proposal_assist.collab import state, store
from call_manager.proposal_assist.collab.checkpoint import serialize_doc_to_markdown
from call_manager.proposal_assist.collab.hydrate import load_or_hydrate
from call_manager.proposal_assist.collab.markdown_bridge import markdown_to_pm
from call_manager.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc
from call_manager.proposal_assist.editing.sectionizer import split_sections
from tests.proposal_assist.conftest import make_workspace
from tests.test_analysis_pipeline import _init_repo

DOC = "# T\nintro\n## Body\nA plain claim.\n"


@pytest.fixture(autouse=True)
def _reset_kill_switch():
    yield
    state.set_kill_switch(False)


def test_annotations_read_reflects_live_room(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        _init_repo(tmp_path, ws.id, DOC)
        state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid = ws.id
    live = pm_json_to_ydoc(markdown_to_pm("# T\n\nintro\n\n## Body\n\nlive rewrite.\n"))
    with pg_session_factory() as s:
        store.append_update(
            s, workspace_id=wid, artifact="draft", actor_id=current_user.id,
            update=live.get_update(),
        )
        s.commit()
    body = client_authed_owner.get(f"/api/workspaces/{wid}/annotations").json()
    live_shas = {sec.sha for sec in split_sections(serialize_doc_to_markdown(live))}
    disk_shas = {sec.sha for sec in split_sections(DOC)}
    got = {s_["section_sha"] for s_ in body["sections"]}
    assert got and got <= live_shas          # every returned sha comes from the ROOM
    assert not (got & (disk_shas - live_shas))  # none from the stale disk doc


def test_untouched_room_reads_disk_and_keeps_preflip_findings(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    """The bug this fix pins: right after enable-on-touch, the room has ONLY
    the unauthored hydration seed (actor_id IS NULL on every row). Serving the
    room's PM-canonical ("loose") projection for that seed-only room would
    mismatch every pre-flip finding's tight, git-HEAD-hashed section_sha and
    render the Claims tab empty. The fix: an untouched room reads raw disk."""
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        _init_repo(tmp_path, ws.id, DOC)
        sec = split_sections(DOC)[1]
        ann = UserClaimAnnotation(
            workspace_id=ws.id,
            section_sha=sec.sha,
            section_occurrence=0,
            section_heading=sec.heading,
            sentence_ordinal=0,
            char_offset=sec.text.index("A plain claim"),
            claim_sha="0" * 64,
            claim_text="A plain claim",
            kind="uncited_no_match",
            evidence_id="",
            status="open",
        )
        s.add(ann)
        state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid = ws.id
        sec_sha = sec.sha
    # Hydrate the room WITHOUT any authored update: seed-only, actor_id IS NULL
    # on the resulting row (mirrors what the FIRST GET of a collab-enabled
    # draft does today via load_or_hydrate).
    with pg_session_factory() as s:
        updates = load_or_hydrate(
            s, workspace_id=wid, artifact="draft", workspaces_dir=str(tmp_path)
        )
        s.commit()
        assert len(updates) == 1  # confirms this is the seed-only, untouched case
        assert not store.has_authored_updates(s, workspace_id=wid, artifact="draft")

    body = client_authed_owner.get(f"/api/workspaces/{wid}/annotations").json()
    by_sha = {sec_["section_sha"]: sec_ for sec_ in body["sections"]}
    assert sec_sha in by_sha  # section resolved against RAW disk text, tight sha intact
    findings = by_sha[sec_sha]["findings"]
    assert findings and findings[0]["claim_text"] == "A plain claim"  # pre-flip finding survives


def test_annotations_read_unchanged_when_disabled(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        _init_repo(tmp_path, ws.id, DOC)
        s.commit()
        wid = ws.id
    body = client_authed_owner.get(f"/api/workspaces/{wid}/annotations").json()
    disk_shas = {sec.sha for sec in split_sections(DOC) if sec.text.strip()}
    got = {s_["section_sha"] for s_ in body["sections"]}
    assert got <= disk_shas and got  # disk-derived, non-empty
