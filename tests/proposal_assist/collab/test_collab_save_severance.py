"""3b-5d.D4 severance: CM6 saves 409 on a collab-enabled artifact (a stale tab
must not git-first-write around the live room); kill-switch restores git-first."""

import subprocess
import uuid

import pytest

from call_manager.proposal_assist.citations.gitio import current_head
from call_manager.proposal_assist.collab import state
from call_manager.proposal_assist.collab.state import CollabSeveranceConflict
from tests.proposal_assist.conftest import make_user, make_workspace
from tests.test_analysis_pipeline import _init_repo

DOC = "# T\nintro\n## Body\nA plain claim.\n"
SAVE_BODY_TEMPLATE = {"content": "## Body\nrewritten.\n"}


@pytest.fixture(autouse=True)
def _reset_kill_switch():
    yield
    state.set_kill_switch(False)


def _seed(s, tmp_path, owner, *, enable=(), with_spec=False):
    ws = make_workspace(s, owner=owner)
    repo = _init_repo(tmp_path, ws.id, DOC)
    if with_spec:
        (repo / "proposal-spec.md").write_text("## Approach\nspec body.\n")
        subprocess.run(["git", "add", "proposal-spec.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "spec"], cwd=repo, check=True)
    for artifact in enable:
        state.enable_collab(s, workspace_id=ws.id, artifact=artifact)
    return ws, repo


def _save_payload(repo, fname="proposal.md", index=1, content="## Body\nrewritten.\n"):
    from call_manager.proposal_assist.citations.gitio import current_head
    from call_manager.proposal_assist.editing.sectionizer import split_sections

    doc = (repo / fname).read_text()
    sec = split_sections(doc)[index]
    return {
        "content": content,
        "base_commit_sha": current_head(repo),
        "base_section_sha": sec.sha,
        "base_branch": "main",
    }


def test_draft_save_409_when_enabled(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, repo = _seed(s, tmp_path, current_user, enable=("draft",))
        s.commit()
        wid = ws.id
    r = client_authed_owner.post(
        f"/api/workspaces/{wid}/sections/1", json=_save_payload(repo)
    )
    assert r.status_code == 409
    assert "live editor" in r.json()["error"]["message"]
    assert (repo / "proposal.md").read_text() == DOC  # nothing written


def test_spec_save_409_when_enabled(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, repo = _seed(s, tmp_path, current_user, enable=("spec",), with_spec=True)
        s.commit()
        wid = ws.id
    r = client_authed_owner.post(
        f"/api/workspaces/{wid}/spec/sections/1",
        json=_save_payload(repo, fname="proposal-spec.md", content="## Approach\nnew.\n"),
    )
    assert r.status_code == 409


def test_draft_save_works_when_only_spec_enabled(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    """Per-artifact: an enabled SPEC must not sever DRAFT saves."""
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, repo = _seed(s, tmp_path, current_user, enable=("spec",), with_spec=True)
        s.commit()
        wid = ws.id
    r = client_authed_owner.post(
        f"/api/workspaces/{wid}/sections/1", json=_save_payload(repo)
    )
    assert r.status_code == 200


def test_kill_switch_restores_git_first_save(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app
):
    """With the switch on, the mask means NO 409 — the CM6 world is back."""
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, repo = _seed(s, tmp_path, current_user, enable=("draft",))
        s.commit()
        wid = ws.id
    state.set_kill_switch(True)
    r = client_authed_owner.post(
        f"/api/workspaces/{wid}/sections/1", json=_save_payload(repo)
    )
    assert r.status_code == 200
    assert "rewritten." in (repo / "proposal.md").read_text()


def test_draft_save_severs_when_flag_flips_after_guard(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app, monkeypatch
):
    """Item 2b: the flag is OFF at the route's pre-mutex severance guard, but
    enable-on-touch flips draft collab on BEFORE save_section's under-mutex
    recheck. The save must abort with the SAME 409 and write NOTHING to git —
    proving the recheck is a real second line of defense past the guard."""
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, repo = _seed(s, tmp_path, current_user)  # flag OFF at seed
        s.commit()
        wid = ws.id

    import call_manager.web.api.workspaces as workspaces_mod

    real_save_section = workspaces_mod.save_section

    def _flip_then_save(*args, **kwargs):
        # Simulate enable-on-touch committing on another connection AFTER the
        # route's pre-mutex guard read False but before save_section's recheck.
        with _app.state.session_factory() as s2:
            state.enable_collab(s2, workspace_id=wid, artifact="draft")
            s2.commit()
        return real_save_section(*args, **kwargs)

    monkeypatch.setattr(workspaces_mod, "save_section", _flip_then_save)
    head_before = current_head(repo)
    r = client_authed_owner.post(
        f"/api/workspaces/{wid}/sections/1", json=_save_payload(repo)
    )
    assert r.status_code == 409
    assert "live editor" in r.json()["error"]["message"]
    assert current_head(repo) == head_before  # git HEAD unchanged
    assert (repo / "proposal.md").read_text() == DOC  # nothing written


def test_spec_save_severs_when_flag_flips_after_guard(
    client_authed_owner, current_user, pg_session_factory, tmp_path, _app, monkeypatch
):
    """Spec twin of test_draft_save_severs_when_flag_flips_after_guard (D4 final-
    review carry): the flag is OFF at the route's pre-mutex severance guard, but
    enable-on-touch flips SPEC collab on BEFORE save_section's under-mutex
    recheck. The save must abort with the SAME 409 and write NOTHING to git —
    proving the recheck is a real second line of defense past the guard on the
    spec artifact too, not just draft."""
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws, repo = _seed(s, tmp_path, current_user, with_spec=True)  # flag OFF at seed
        s.commit()
        wid = ws.id

    import call_manager.web.api.workspaces as workspaces_mod

    real_save_section = workspaces_mod.save_section

    def _flip_then_save(*args, **kwargs):
        # Simulate enable-on-touch committing on another connection AFTER the
        # route's pre-mutex guard read False but before save_section's recheck.
        with _app.state.session_factory() as s2:
            state.enable_collab(s2, workspace_id=wid, artifact="spec")
            s2.commit()
        return real_save_section(*args, **kwargs)

    monkeypatch.setattr(workspaces_mod, "save_section", _flip_then_save)
    spec_before = (repo / "proposal-spec.md").read_text()
    head_before = current_head(repo)
    r = client_authed_owner.post(
        f"/api/workspaces/{wid}/spec/sections/1",
        json=_save_payload(repo, fname="proposal-spec.md", content="## Approach\nnew.\n"),
    )
    assert r.status_code == 409
    assert "live editor" in r.json()["error"]["message"]
    assert current_head(repo) == head_before  # git HEAD unchanged
    assert (repo / "proposal-spec.md").read_text() == spec_before  # nothing written


def test_post_citation_git_path_severed_when_flag_flips_after_guard(
    pg_session_factory, tmp_path
):
    """Item 2b follow-up: insert_citation_for_user (post_citation's git-first
    branch) rechecks the flag UNDER the mutex. Flag enabled by the time it runs
    (the route's _route() fork raced enable-on-touch) -> CollabSeveranceConflict
    (the route maps it to the 409 COLLAB_SEVERED_DETAIL), git HEAD unchanged.
    White-box direct call: the recheck fires before any reference/section
    validation, so a bogus reference_id proves nothing downstream ever ran."""
    from call_manager.proposal_assist.citations.human_cite import insert_citation_for_user

    with pg_session_factory() as s:
        ws, repo = _seed(s, tmp_path, make_user(s, email="sever-cite@umu.se"), enable=("draft",))
        s.commit()
        head_before = current_head(repo)
        with pytest.raises(CollabSeveranceConflict):
            insert_citation_for_user(
                session=s,
                workspace=ws,
                user_id=ws.owner_id,
                section_index=1,
                base_commit_sha=head_before,
                base_section_sha="0" * 64,
                anchor="A plain claim.",
                reference_id=uuid.uuid4(),
                chunk_index=0,
                evidence_quote="anything",
                quote_start=0,
                quote_end=8,
                workspaces_dir=str(tmp_path),
            )
        s.rollback()
    assert current_head(repo) == head_before  # nothing written
    assert (repo / "proposal.md").read_text() == DOC


def test_citation_accept_replay_severed_when_flag_flips_after_guard(
    pg_session_factory, tmp_path
):
    """Item 2b follow-up: the git-first citation-accept replay (_accept_git_first
    -> accept_proposed_change -> _accept_citation_insert -> _insert_citation_locked)
    rechecks the flag UNDER the mutex. Flag enabled -> CollabSeveranceConflict
    RAISES (never the terminal error-dict path), so git is untouched AND the
    ProposedChange row stays PENDING — retryable via the kernel accept after the
    client reloads, never burned to 'stale'."""
    from call_manager.proposal_assist.editing.sectionizer import split_sections
    from call_manager.proposal_assist.ports.stamp import FenceStamp
    from call_manager.proposal_assist.proposals.accepting import accept_proposed_change
    from call_manager.proposal_assist.proposals.proposing import propose_citation_insert

    with pg_session_factory() as s:
        owner = make_user(s, email="sever-accept@umu.se")
        ws, repo = _seed(s, tmp_path, owner, enable=("draft",))
        sec = split_sections(DOC)[1]
        pc = propose_citation_insert(
            session=s,
            workspace_id=ws.id,
            section_index=1,
            base_commit_sha=current_head(repo),
            base_section_sha=sec.sha,
            reference_id=str(uuid.uuid4()),  # never dereferenced: recheck fires first
            anchor="A plain claim.",
            chunk_index=0,
            evidence_quote="anything",
            origin_kind="interactive_turn",
        )
        s.commit()
        pc_id, wid = pc.id, ws.id
        head_before = current_head(repo)
    with pg_session_factory() as s:
        approver = make_user(s, email="sever-approver@umu.se")
        s.flush()
        # The workspace is live at generation 0; the stamp the route would thread
        # from require_member. The severance recheck runs UNDER the mutex, so the
        # stamp must let acquire_commit_mutex_at_generation succeed first.
        stamp = FenceStamp(
            workspace_id=wid, generation=0, active_turn_job_id=None, origin_job_id=None
        )
        with pytest.raises(CollabSeveranceConflict):
            accept_proposed_change(
                session=s,
                proposed_change_id=pc_id,
                workspace_id=wid,
                approver=approver,
                workspaces_dir=str(tmp_path),
                stamp=stamp,
            )
        s.rollback()
    assert current_head(repo) == head_before  # git untouched
    with pg_session_factory() as s:
        from call_manager.db.models import ProposedChange

        assert s.get(ProposedChange, pc_id).status == "pending"  # NOT burned to stale
