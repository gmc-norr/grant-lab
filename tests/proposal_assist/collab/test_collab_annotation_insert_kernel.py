"""3b-5d.D4: Claims-tab insert routes through the KERNEL when draft collab is on —
the marker lands in the LIVE room + ledger + checkpoint; annotation flips to
'inserted'; second call is idempotent."""

import pytest

from call_manager.db.models import UserClaimAnnotation, WorkspaceEvent
from call_manager.proposal_assist.collab import state
from call_manager.proposal_assist.collab.canonical import canonical_artifact_markdown
from call_manager.proposal_assist.paths import workspace_repo_dir
from tests.proposal_assist.test_api_annotations_insert import _seed_supported


@pytest.fixture(autouse=True)
def _reset_kill_switch():
    yield
    state.set_kill_switch(False)


def test_enabled_insert_lands_in_room_and_flips_annotation(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, ann_id, repo = _seed_supported(s, tmp_path, current_user)
        state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid = ws.id
    r = client_authed_owner.post(f"/api/workspaces/{wid}/annotations/{ann_id}/insert")
    assert r.status_code == 200
    body = r.json()
    assert body["ev_id"].startswith("ev-") and body["commit_sha"]
    with pg_session_factory() as s:
        canon = canonical_artifact_markdown(
            s, workspace_id=wid, artifact="draft",
            repo_dir=workspace_repo_dir(str(tmp_path), wid),
        )
        assert f"ev:{body['ev_id']}" in canon  # marker is IN THE ROOM projection
        ann = s.get(UserClaimAnnotation, ann_id)
        assert ann.status == "inserted"
        # provenance parity with the git path (Codex Medium): resolved fields +
        # the user_citation_insert feed event survive the kernel routing
        assert ann.resolved_by_user_id == current_user.id
        assert ann.resolved_at is not None
        ev_kinds = [
            e.kind
            for e in s.query(WorkspaceEvent).filter_by(workspace_id=wid).all()
        ]
        assert "user_citation_insert" in ev_kinds
        # ledger row carries the annotation's claim context (bespoke core, not
        # the generic citation row shape)
        import json
        ledger = json.loads(
            (workspace_repo_dir(str(tmp_path), wid) / "citations.json").read_text()
        )
        row = next(r for r in ledger["evidence"] if r["id"] == body["ev_id"])
        assert (row.get("claim_context") or {}).get("hash")
    # idempotent repeat
    r2 = client_authed_owner.post(f"/api/workspaces/{wid}/annotations/{ann_id}/insert")
    assert r2.status_code == 200 and r2.json()["idempotent"] is True


def test_same_evidence_elsewhere_still_inserts_here(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    """THE occurrence-aware regression (Codex High): the same (ref, chunk,
    quote) already cited in ANOTHER location must NOT satisfy this annotation —
    the kernel path inserts a SECOND marker + ledger row, never a false
    idempotent. Build the pre-existing citation by running the git-path insert
    for a FIRST annotation, then enable collab and insert a SECOND annotation
    for the same evidence at a different sentence."""
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, ann1_id, repo = _seed_supported(s, tmp_path, current_user)
        s.commit()
        wid = ws.id
    # first insert via the git path (flag off) — evidence now live in the doc
    r1 = client_authed_owner.post(f"/api/workspaces/{wid}/annotations/{ann1_id}/insert")
    assert r1.status_code == 200
    ev1 = r1.json()["ev_id"]
    # second OPEN annotation, same reference/chunk/quote, different sentence:
    # clone ann1's row against another claim sentence appended to the section
    with pg_session_factory() as s:
        from call_manager.proposal_assist.editing.sectionizer import split_sections

        a1 = s.get(UserClaimAnnotation, ann1_id)
        doc = (repo / "proposal.md").read_text()
        new_doc = doc + "Another factual claim.\n"
        import subprocess
        (repo / "proposal.md").write_text(new_doc)
        subprocess.run(["git", "add", "proposal.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "second claim"], cwd=repo, check=True)
        sec = split_sections(new_doc)[1]
        a2 = UserClaimAnnotation(
            workspace_id=wid, section_sha=sec.sha, section_occurrence=0,
            section_heading=sec.heading, sentence_ordinal=1,
            char_offset=sec.text.index("Another factual claim"),
            claim_sha="1" * 64, claim_text="Another factual claim",
            kind="uncited_supported", evidence_id="",
            reference_id=a1.reference_id, chunk_index=a1.chunk_index,
            evidence_quote=a1.evidence_quote, status="open",
        )
        s.add(a2)
        from call_manager.proposal_assist.collab import state as collab_state_mod
        collab_state_mod.enable_collab(s, workspace_id=wid, artifact="draft")
        s.commit()
        ann2_id = a2.id
    r2 = client_authed_owner.post(f"/api/workspaces/{wid}/annotations/{ann2_id}/insert")
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2.get("idempotent") is not True  # NOT falsely satisfied
    assert body2["ev_id"] != ev1                # a genuinely NEW citation
    with pg_session_factory() as s:
        canon = canonical_artifact_markdown(
            s, workspace_id=wid, artifact="draft",
            repo_dir=workspace_repo_dir(str(tmp_path), wid),
        )
    assert f"ev:{ev1}" in canon and f"ev:{body2['ev_id']}" in canon  # BOTH markers live


def test_indexerror_maps_to_409_and_does_not_burn_annotation(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app, monkeypatch
):
    """Sibling parity (workspaces.py's post_citation kernel branch mapping
    IndexError -> 409): a bare IndexError surfacing from
    insert_annotation_via_kernel (e.g. a peer's concurrent edit shrank the
    section map mid-insert) must map to a 409, not an unhandled 500 — and the
    annotation must NOT be burned to a terminal status by a request that never
    actually inserted anything."""
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, ann_id, repo = _seed_supported(s, tmp_path, current_user)
        state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid = ws.id

    import call_manager.web.api.annotations as annotations_mod

    async def _boom(**kwargs):
        raise IndexError("section map shrank underneath the annotation")

    monkeypatch.setattr(annotations_mod, "insert_annotation_via_kernel", _boom)
    r = client_authed_owner.post(f"/api/workspaces/{wid}/annotations/{ann_id}/insert")
    assert r.status_code == 409
    with pg_session_factory() as s:
        assert s.get(UserClaimAnnotation, ann_id).status == "open"  # not burned


def test_disabled_insert_takes_git_path(  # regression pin for the fork
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, ann_id, repo = _seed_supported(s, tmp_path, current_user)
        s.commit()
        wid = ws.id
    r = client_authed_owner.post(f"/api/workspaces/{wid}/annotations/{ann_id}/insert")
    assert r.status_code == 200
    assert "<!-- ev:ev-" in (repo / "proposal.md").read_text()  # git-first, disk mutated
