# tests/proposal_assist/collab/test_direct_cite_via_kernel.py
from pathlib import Path

import pytest

from call_manager.proposal_assist.citations.collab_cite import direct_cite_via_kernel
from call_manager.proposal_assist.citations.human_cite import CiteStale
from call_manager.proposal_assist.citations.ledger import load_ledger
from call_manager.proposal_assist.collab import state
from call_manager.proposal_assist.collab.hydrate import build_doc, load_or_hydrate
from call_manager.proposal_assist.collab.markdown_bridge import pm_to_markdown
from call_manager.proposal_assist.collab.room import MANAGER
from call_manager.proposal_assist.collab.yjs_bridge import ydoc_to_pm_json
from call_manager.proposal_assist.editing.save import CITATIONS_FILENAME, PROPOSAL_FILENAME
from call_manager.proposal_assist.editing.sectionizer import split_sections
from tests.proposal_assist.collab.test_collab_kernel_citation import _init_repo, _seed_reference
from tests.proposal_assist.conftest import make_user, make_workspace, stamp_for


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _base_sha(session, wid, workspaces_dir, section_index=1):
    updates = load_or_hydrate(
        session, stamp=stamp_for(session, wid), workspace_id=wid, artifact="draft",
        workspaces_dir=workspaces_dir
    )
    session.commit()
    room_doc = build_doc(updates)
    return split_sections(pm_to_markdown(ydoc_to_pm_json(room_doc)))[section_index].sha


@pytest.mark.anyio
async def test_direct_cite_applies_and_returns(pg_session_factory, tmp_path):
    md0 = "preamble\n\n## Methods\nThe claim here.\n"
    with pg_session_factory() as s:
        o = make_user(s, email="dck@umu.se")
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
        bsha = _base_sha(s, wid, str(tmp_path))
        stamp = stamp_for(s, wid)
    out = await direct_cite_via_kernel(
        session_factory=pg_session_factory, workspaces_dir=str(tmp_path), stamp=stamp,
        workspace_id=wid, actor_id=oid, section_index=1, base_section_sha=bsha,
        anchor="The claim here.", reference_id=rid, chunk_index=0, evidence_quote="azure",
        quote_start=0, quote_end=5, stance="supports")
    assert set(out) == {"ev_id", "citekey", "commit_sha"}
    disk = (repo / PROPOSAL_FILENAME).read_text()
    assert f"[@{out['citekey']}]<!-- ev:{out['ev_id']} -->" in disk
    assert load_ledger((repo / CITATIONS_FILENAME).read_text())["evidence"][0]["id"] == out["ev_id"]


@pytest.mark.anyio
async def test_direct_cite_anchor_gone_is_clean_stale_no_evict(
    pg_session_factory, tmp_path, monkeypatch
):
    # D1b: the direct path no longer enforces base_section_sha, so "stale" is now
    # surfaced by a GONE anchor (require_anchor_in_section), not a bad sha. It must
    # still be a CLEAN pre-mutation error → no eviction, no lingering room.
    md0 = "preamble\n\n## Methods\nThe claim here.\n"
    with pg_session_factory() as s:
        o = make_user(s, email="dck2@umu.se")
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
        bsha = _base_sha(s, wid, str(tmp_path))
        stamp = stamp_for(s, wid)
    evict_calls: list = []
    _orig_evict = MANAGER.evict
    async def _spy_evict(key):
        evict_calls.append(key)
        return await _orig_evict(key)
    monkeypatch.setattr(MANAGER, "evict", _spy_evict)
    with pytest.raises(CiteStale):
        await direct_cite_via_kernel(
            session_factory=pg_session_factory, workspaces_dir=str(tmp_path), stamp=stamp,
            workspace_id=wid, actor_id=oid, section_index=1, base_section_sha=bsha,
            anchor="NOT PRESENT IN THIS SECTION", reference_id=rid, chunk_index=0,
            evidence_quote="azure", quote_start=0, quote_end=5, stance="supports")
    assert evict_calls == []  # clean error → evict must NOT have been called
    assert MANAGER.get((wid, "draft")) is None  # clean abort → no lingering room


@pytest.mark.anyio
async def test_direct_cite_post_mutation_failure_evicts(pg_session_factory, tmp_path, monkeypatch):
    md0 = "preamble\n\n## Methods\nThe claim here.\n"
    with pg_session_factory() as s:
        o = make_user(s, email="dck3@umu.se")
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
        bsha = _base_sha(s, wid, str(tmp_path))
        stamp = stamp_for(s, wid)
    # Fail AFTER the fragment mutation (during checkpoint) → room.doc ahead of log → evict.
    import call_manager.proposal_assist.collab.kernel as kmod
    def _boom(*a, **k): raise RuntimeError("disk full")
    monkeypatch.setattr(kmod, "checkpoint_artifact", _boom)
    evict_calls: list = []
    _orig_evict = MANAGER.evict
    async def _spy_evict(key):
        evict_calls.append(key)
        return await _orig_evict(key)
    monkeypatch.setattr(MANAGER, "evict", _spy_evict)
    with pytest.raises(RuntimeError):
        await direct_cite_via_kernel(
            session_factory=pg_session_factory, workspaces_dir=str(tmp_path), stamp=stamp,
            workspace_id=wid, actor_id=oid, section_index=1, base_section_sha=bsha,
            anchor="The claim here.", reference_id=rid, chunk_index=0, evidence_quote="azure",
            quote_start=0, quote_end=5, stance="supports")
    assert (wid, "draft") in evict_calls  # post-mutation error → evict must have been called
    assert len(evict_calls) == 1
    assert MANAGER.get((wid, "draft")) is None  # evicted (poison recovery)


@pytest.mark.anyio
async def test_direct_cite_section_structure_error_does_not_evict(
    pg_session_factory, tmp_path, monkeypatch
):
    # Review A: a pre-mutation SectionStructureError (e.g. a section-map divergence in
    # the apply) must be re-raised WITHOUT evicting a clean room — it belongs in
    # _CLEAN_CITE_ERRORS, mirroring _CLEAN_ACCEPT_ERRORS.
    import call_manager.proposal_assist.citations.collab_cite as ccmod
    from call_manager.proposal_assist.editing.save import SectionStructureError

    md0 = "preamble\n\n## Methods\nThe claim here.\n"
    with pg_session_factory() as s:
        o = make_user(s, email="dck5@umu.se")
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
        bsha = _base_sha(s, wid, str(tmp_path))
        stamp = stamp_for(s, wid)

    def _raise_struct(*a, **k):
        raise SectionStructureError("section-map divergence")

    monkeypatch.setattr(ccmod, "apply_citation_to_room", _raise_struct)
    evict_calls: list = []
    _orig_evict = MANAGER.evict

    async def _spy_evict(key):
        evict_calls.append(key)
        return await _orig_evict(key)

    monkeypatch.setattr(MANAGER, "evict", _spy_evict)
    with pytest.raises(SectionStructureError):
        await direct_cite_via_kernel(
            session_factory=pg_session_factory, workspaces_dir=str(tmp_path), stamp=stamp,
            workspace_id=wid, actor_id=oid, section_index=1, base_section_sha=bsha,
            anchor="The claim here.", reference_id=rid, chunk_index=0, evidence_quote="azure",
            quote_start=0, quote_end=5, stance="supports")
    assert evict_calls == []  # pre-mutation clean error → no evict


@pytest.mark.anyio
async def test_direct_cite_stale_sha_allowed_when_anchor_present(pg_session_factory, tmp_path):
    # D1b THE regression: a mismatched/stale base_section_sha (the toolbar's git sha,
    # while the live room is ahead) with a still-present anchor SUCCEEDS — direct_cite
    # passes enforce_section_sha=False and relies on anchor-relocation.
    md0 = "preamble\n\n## Methods\nThe claim here.\n"
    with pg_session_factory() as s:
        o = make_user(s, email="dck6@umu.se")
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
    with pg_session_factory() as _s:
        stamp = stamp_for(_s, wid)
    out = await direct_cite_via_kernel(
        session_factory=pg_session_factory, workspaces_dir=str(tmp_path), stamp=stamp,
        workspace_id=wid, actor_id=oid, section_index=1,
        base_section_sha="dead" * 16,  # STALE — now ignored
        anchor="The claim here.", reference_id=rid, chunk_index=0,
        evidence_quote="azure", quote_start=0, quote_end=5, stance="supports")
    assert set(out) == {"ev_id", "citekey", "commit_sha"}
    disk = (repo / PROPOSAL_FILENAME).read_text()
    assert f"[@{out['citekey']}]<!-- ev:{out['ev_id']} -->" in disk
