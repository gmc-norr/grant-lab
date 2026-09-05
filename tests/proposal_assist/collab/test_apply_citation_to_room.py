# tests/proposal_assist/collab/test_apply_citation_to_room.py
from pathlib import Path

import pytest

from grant_lab.proposal_assist.citations.human_cite import CiteStale
from grant_lab.proposal_assist.citations.ledger import load_ledger
from grant_lab.proposal_assist.collab import state
from grant_lab.proposal_assist.collab.hydrate import build_doc, load_or_hydrate
from grant_lab.proposal_assist.collab.kernel import apply_citation_to_room
from grant_lab.proposal_assist.collab.markdown_bridge import pm_to_markdown
from grant_lab.proposal_assist.collab.yjs_bridge import ydoc_to_pm_json
from grant_lab.proposal_assist.editing.save import CITATIONS_FILENAME, PROPOSAL_FILENAME
from grant_lab.proposal_assist.editing.sectionizer import split_sections

# Reuse the citation-kernel harness helpers.
from tests.proposal_assist.collab.test_collab_kernel_citation import _init_repo, _seed_reference
from tests.proposal_assist.conftest import make_user, make_workspace, stamp_for


def _live_room(session, wid, workspaces_dir):
    updates = load_or_hydrate(
        session, stamp=stamp_for(session, wid), workspace_id=wid, artifact="draft",
        workspaces_dir=workspaces_dir
    )
    session.commit()
    return build_doc(updates)


def test_apply_citation_to_room_inserts_marker_and_cocommits_ledger(pg_session_factory, tmp_path):
    md0 = "preamble\n\n## Methods\nThe claim here.\n"
    with pg_session_factory() as s:
        o = make_user(s, email="acr@umu.se")
        ws = make_workspace(s, owner=o, current_phase="active")
        s.commit()
        wid, oid = ws.id, o.id
    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, md0)
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()
        rid = _seed_reference(s, wid, chunk_text="azure sky").id
        s.commit()
    with pg_session_factory() as s:
        room_doc = _live_room(s, wid, str(tmp_path))
    base_sha = split_sections(pm_to_markdown(ydoc_to_pm_json(room_doc)))[1].sha
    with pg_session_factory() as s:
        res = apply_citation_to_room(
            s, stamp=stamp_for(s, wid), room_doc=room_doc, workspace_id=wid, section_index=1,
            base_section_sha=base_sha, anchor="The claim here.", reference_id=rid,
            chunk_index=0, evidence_quote="azure", quote_start=0, quote_end=5,
            actor_id=oid, workspaces_dir=str(tmp_path), require_anchor_in_section=True)
        s.commit()
    assert res.update is not None and res.ev_id and res.citekey
    disk = (repo / PROPOSAL_FILENAME).read_text()
    assert f"[@{res.citekey}]<!-- ev:{res.ev_id} -->" in disk
    ledger = load_ledger((repo / CITATIONS_FILENAME).read_text())
    assert ledger["evidence"][0]["id"] == res.ev_id


def test_apply_citation_stale_section_sha_raises(pg_session_factory, tmp_path):
    md0 = "preamble\n\n## Methods\nThe claim here.\n"
    with pg_session_factory() as s:
        o = make_user(s, email="acr2@umu.se")
        ws = make_workspace(s, owner=o, current_phase="active")
        s.commit()
        wid, oid = ws.id, o.id
    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, md0)
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()
        rid = _seed_reference(s, wid, chunk_text="azure sky").id
        s.commit()
    with pg_session_factory() as s:
        room_doc = _live_room(s, wid, str(tmp_path))
    with pg_session_factory() as s, pytest.raises(CiteStale):
        apply_citation_to_room(
            s, stamp=stamp_for(s, wid), room_doc=room_doc, workspace_id=wid, section_index=1,
            base_section_sha="deadbeef" * 8, anchor="The claim here.", reference_id=rid,
            chunk_index=0, evidence_quote="azure", quote_start=0, quote_end=5,
            actor_id=oid, workspaces_dir=str(tmp_path), require_anchor_in_section=True)


def test_apply_citation_anchor_absent_from_section_raises(pg_session_factory, tmp_path):
    md0 = "preamble\n\n## Methods\nThe claim here.\n"
    with pg_session_factory() as s:
        o = make_user(s, email="acr3@umu.se")
        ws = make_workspace(s, owner=o, current_phase="active")
        s.commit()
        wid, oid = ws.id, o.id
    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, md0)
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()
        rid = _seed_reference(s, wid, chunk_text="azure sky").id
        s.commit()
    with pg_session_factory() as s:
        room_doc = _live_room(s, wid, str(tmp_path))
    base_sha = split_sections(pm_to_markdown(ydoc_to_pm_json(room_doc)))[1].sha
    with pg_session_factory() as s, pytest.raises(CiteStale):   # anchor not in section 1
        apply_citation_to_room(
            s, stamp=stamp_for(s, wid), room_doc=room_doc, workspace_id=wid, section_index=1,
            base_section_sha=base_sha, anchor="NOT PRESENT ANYWHERE", reference_id=rid,
            chunk_index=0, evidence_quote="azure", quote_start=0, quote_end=5,
            actor_id=oid, workspaces_dir=str(tmp_path), require_anchor_in_section=True)


def test_apply_citation_anchor_unique_in_other_section_rejected(pg_session_factory, tmp_path):
    # The distinguishing case (Codex): the anchor is absent from the SELECTED section
    # but occurs exactly ONCE in another section → whole-doc insert_marker would
    # SUCCEED and mis-cite that other section. Only the section pre-check catches it;
    # a broken `== 0` guard lets this through and the test fails (no CiteStale raised).
    md0 = "preamble\n\n## Methods\nAlpha body here.\n\n## Results\nUNIQUE PHRASE lives here.\n"
    with pg_session_factory() as s:
        o = make_user(s, email="acr4@umu.se")
        ws = make_workspace(s, owner=o, current_phase="active")
        s.commit()
        wid, oid = ws.id, o.id
    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, md0)
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()
        rid = _seed_reference(s, wid, chunk_text="azure sky").id
        s.commit()
    with pg_session_factory() as s:
        room_doc = _live_room(s, wid, str(tmp_path))
    sec1_sha = split_sections(pm_to_markdown(ydoc_to_pm_json(room_doc)))[1].sha
    # "UNIQUE PHRASE" is in section 2, not section 1
    with pg_session_factory() as s, pytest.raises(CiteStale):
        apply_citation_to_room(
                s, stamp=stamp_for(s, wid), room_doc=room_doc, workspace_id=wid, section_index=1,
                base_section_sha=sec1_sha, anchor="UNIQUE PHRASE", reference_id=rid,
                chunk_index=0, evidence_quote="azure", quote_start=0, quote_end=5,
                actor_id=oid, workspaces_dir=str(tmp_path), require_anchor_in_section=True)
