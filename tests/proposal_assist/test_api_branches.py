"""Branch routes. Owner-only for mutation, and every refusal maps to a status the
UI can act on rather than a 500.

Fixture shapes, measured from tests/proposal_assist/conftest.py:
  * `client_authed_owner` yields the TestClient ALONE (not a tuple).
  * `current_user` is the logged-in user.
  * `_app` is the app; `_app.state.settings.workspaces_dir` is where repos live.
  * A non-owner 403 is built by giving the workspace to a DIFFERENT user — the
    pattern in test_api_annotations_dismiss.py:82-92.
"""

import pytest

from grant_lab.db.models import Branch, Workspace
from grant_lab.proposal_assist.paths import workspace_repo_dir
from tests.proposal_assist.conftest import init_workspace_repo, make_user, make_workspace


def _seed(session, app, owner):
    """A workspace owned by *owner*, with a real repo on `main`."""
    ws = make_workspace(session, title="w", owner=owner)
    session.flush()
    init_workspace_repo(
        workspace_repo_dir(app.state.settings.workspaces_dir, ws.id), "# Aims\n\nbody\n"
    )
    return ws.id


def test_list_returns_branches_and_current(
    client_authed_owner, current_user, pg_session_factory, _app
):
    """Deliberately EXACT equality, not a subset check. This is the only
    assertion in the file that notices a response-shape change at all, and a
    subset assertion is a gate that cannot fail."""
    from grant_lab.proposal_assist.citations.gitio import branch_tips

    with pg_session_factory() as s:
        wid = _seed(s, _app, current_user)
        s.commit()
    repo = workspace_repo_dir(_app.state.settings.workspaces_dir, wid)

    r = client_authed_owner.get(f"/api/workspaces/{wid}/branches")
    assert r.status_code == 200
    assert r.json() == {
        "branches": ["main"],
        "current": "main",
        "tips": {"main": branch_tips(repo)["main"]},
    }


def test_create_then_list(client_authed_owner, current_user, pg_session_factory, _app):
    with pg_session_factory() as s:
        wid = _seed(s, _app, current_user)
        s.commit()
    assert (
        client_authed_owner.post(f"/api/workspaces/{wid}/branches", json={"name": "v2"}).status_code
        == 201
    )
    assert sorted(
        client_authed_owner.get(f"/api/workspaces/{wid}/branches").json()["branches"]
    ) == ["main", "v2"]


def test_create_with_an_invalid_name_is_422_not_500(
    client_authed_owner, current_user, pg_session_factory, _app
):
    with pg_session_factory() as s:
        wid = _seed(s, _app, current_user)
        s.commit()
    assert (
        client_authed_owner.post(
            f"/api/workspaces/{wid}/branches", json={"name": "a..b"}
        ).status_code
        == 422
    )


def test_create_a_duplicate_is_409(client_authed_owner, current_user, pg_session_factory, _app):
    with pg_session_factory() as s:
        wid = _seed(s, _app, current_user)
        s.commit()
    client_authed_owner.post(f"/api/workspaces/{wid}/branches", json={"name": "v2"})
    assert (
        client_authed_owner.post(f"/api/workspaces/{wid}/branches", json={"name": "v2"}).status_code
        == 409
    )


def test_switch_moves_the_workspace(client_authed_owner, current_user, pg_session_factory, _app):
    with pg_session_factory() as s:
        wid = _seed(s, _app, current_user)
        s.commit()
    client_authed_owner.post(f"/api/workspaces/{wid}/branches", json={"name": "v2"})
    r = client_authed_owner.post(f"/api/workspaces/{wid}/branches/switch", json={"name": "v2"})
    assert r.status_code == 200
    assert r.json()["current"] == "v2"


def test_switch_to_a_missing_branch_is_404(
    client_authed_owner, current_user, pg_session_factory, _app
):
    with pg_session_factory() as s:
        wid = _seed(s, _app, current_user)
        s.commit()
    assert (
        client_authed_owner.post(
            f"/api/workspaces/{wid}/branches/switch", json={"name": "ghost"}
        ).status_code
        == 404
    )


def test_delete_the_current_branch_is_409(
    client_authed_owner, current_user, pg_session_factory, _app
):
    with pg_session_factory() as s:
        wid = _seed(s, _app, current_user)
        s.commit()
    client_authed_owner.post(f"/api/workspaces/{wid}/branches", json={"name": "v2"})
    assert client_authed_owner.delete(f"/api/workspaces/{wid}/branches/main").status_code == 409


def test_delete_works_for_a_name_containing_a_slash(
    client_authed_owner, current_user, pg_session_factory, _app
):
    # `aims/ambitious` is a valid branch name (task 1). With the default `{name}`
    # converter this route would 404 — a branch you can create and cannot delete.
    with pg_session_factory() as s:
        wid = _seed(s, _app, current_user)
        s.commit()
    client_authed_owner.post(f"/api/workspaces/{wid}/branches", json={"name": "aims/ambitious"})
    assert (
        client_authed_owner.delete(f"/api/workspaces/{wid}/branches/aims/ambitious").status_code
        == 204
    )


def test_delete_removes_it(client_authed_owner, current_user, pg_session_factory, _app):
    with pg_session_factory() as s:
        wid = _seed(s, _app, current_user)
        s.commit()
    client_authed_owner.post(f"/api/workspaces/{wid}/branches", json={"name": "v2"})
    assert client_authed_owner.delete(f"/api/workspaces/{wid}/branches/v2").status_code == 204
    assert client_authed_owner.get(f"/api/workspaces/{wid}/branches").json()["branches"] == ["main"]


MUTATIONS = [
    ("post", "/branches", {"name": "x"}),
    ("post", "/branches/switch", {"name": "main"}),
    ("delete", "/branches/main", None),
]


@pytest.mark.parametrize("verb,suffix,body", MUTATIONS)
def test_a_stranger_cannot_mutate_branches(
    client_authed_owner, pg_session_factory, _app, verb, suffix, body
):
    # Not the owner and not a member: refused by either guard.
    with pg_session_factory() as s:
        stranger = make_user(s, email="stranger-branch@umu.se")
        wid = _seed(s, _app, stranger)
        s.commit()
    call = getattr(client_authed_owner, verb)
    r = call(f"/api/workspaces/{wid}{suffix}", **({"json": body} if body is not None else {}))
    assert r.status_code == 403


@pytest.mark.parametrize("verb,suffix,body", MUTATIONS)
def test_a_MEMBER_can_list_but_cannot_mutate(
    client_authed_owner, current_user, pg_session_factory, _app, verb, suffix, body
):
    # THE test that makes the owner gate observable. A stranger is refused by
    # require_member too, so a stranger-only test passes even if the route were
    # downgraded to require_member — it would prove nothing about "owner only".
    # `require_member` = owner OR a workspace_viewers row (_perms.py:29-45).
    from grant_lab.db.models import WorkspaceViewer

    with pg_session_factory() as s:
        other_owner = make_user(s, email="branch-owner@umu.se")
        wid = _seed(s, _app, other_owner)
        s.add(WorkspaceViewer(workspace_id=wid, user_id=current_user.id))
        s.commit()

    # A member may READ.
    assert client_authed_owner.get(f"/api/workspaces/{wid}/branches").status_code == 200
    # And may not WRITE (spec §7: owner only for create, switch, delete).
    call = getattr(client_authed_owner, verb)
    r = call(f"/api/workspaces/{wid}{suffix}", **({"json": body} if body is not None else {}))
    assert r.status_code == 403


def test_the_api_refuses_to_delete_the_root_branch(
    client_authed_owner, current_user, pg_session_factory, _app
):
    """The ONE deliberate behaviour change in this plan (see Global Constraints).
    Today a user who switched away from the root can delete it; §7.3 closes that.
    Pinned at the API because the response code is what actually changed — and
    `{name:path}` means the route itself is easy to get wrong.

    `_seed` only runs `init_workspace_repo` (a bare `git init`); it does not found
    a root branch row for the new workspace (a known gap, handled by a later
    task). So the root is established explicitly here, in the Arrange step, where
    it is visible — this test is about the root, so the root should not be hidden
    in a helper.
    """
    with pg_session_factory() as s:
        wid = _seed(s, _app, current_user)
        s.flush()
        root = Branch(workspace_id=wid, name="main")
        s.add(root)
        s.flush()
        s.get(Workspace, wid).root_branch_id = root.id
        s.commit()

    client_authed_owner.post(f"/api/workspaces/{wid}/branches", json={"name": "new_ideas"})
    client_authed_owner.post(f"/api/workspaces/{wid}/branches/switch", json={"name": "new_ideas"})

    resp = client_authed_owner.delete(f"/api/workspaces/{wid}/branches/main")
    assert resp.status_code == 409, resp.text


def test_listing_branches_for_a_workspace_still_in_setup_is_409_not_500(
    client_authed_owner, current_user, pg_session_factory, _app
):
    """A workspace whose repo Phase 0 has not created yet is YOUNG, not broken.

    `list_branches_route` used to be the one route in this module with no
    exception handler, so `subprocess.run(cwd=<missing dir>)` raised a bare
    FileNotFoundError (measured) straight into main.py's generic handler. The
    cockpit's read path answered 500 for every workspace between creation and
    `_init_repo`.

    409, not 404: the workspace is real, it is just not ready — a 404 would tell
    the SPA to stop asking.
    """
    with pg_session_factory() as s:
        # Deliberately NOT `_seed`: no repo is the whole point.
        wid = make_workspace(s, title="young", owner=current_user).id
        s.commit()
    r = client_authed_owner.get(f"/api/workspaces/{wid}/branches")
    assert r.status_code == 409, r.text


def test_create_from_sha_at_the_api(client_authed_owner, current_user, pg_session_factory, _app):
    """The route must FORWARD from_sha, so the branch has to land somewhere HEAD
    is not.

    `_seed` builds a repo with ONE commit. Branching from that commit's sha is
    the same as branching from HEAD, so a route that drops `from_sha` entirely
    and lets the service default to HEAD would pass. A second commit is what
    makes the assertion mean anything.
    """
    import subprocess

    from grant_lab.proposal_assist.citations.gitio import branch_tips, current_head

    with pg_session_factory() as s:
        wid = _seed(s, _app, current_user)
        s.commit()
    repo = workspace_repo_dir(_app.state.settings.workspaces_dir, wid)
    older = current_head(repo)

    (repo / "proposal.md").write_text("# Aims\n\nmore\n", encoding="utf-8")
    for argv in (["add", "proposal.md"], ["commit", "-q", "-m", "newer"]):
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", *argv], cwd=repo, check=True
        )
    newer = current_head(repo)
    assert older != newer

    r = client_authed_owner.post(
        f"/api/workspaces/{wid}/branches", json={"name": "v2", "from_sha": older}
    )
    assert r.status_code == 201, r.text
    tips = branch_tips(repo)
    assert tips["v2"] == older, "the route dropped from_sha and branched at HEAD"
    assert tips["main"] == newer


def test_create_from_an_unknown_sha_is_404(
    client_authed_owner, current_user, pg_session_factory, _app
):
    with pg_session_factory() as s:
        wid = _seed(s, _app, current_user)
        s.commit()
    r = client_authed_owner.post(
        f"/api/workspaces/{wid}/branches", json={"name": "v2", "from_sha": "0" * 40}
    )
    assert r.status_code == 404, r.text


def test_the_draft_reports_the_branch_it_is_actually_on(
    client_authed_owner, current_user, pg_session_factory, _app
):
    """`branch` must come from the workspace row, not a constant.

    Every other assertion of this field in the suite compares it to "main" --
    which is BOTH the server_default of `workspaces.current_branch`
    (models.py:704) and the branch `init_workspace_repo` creates. So replacing
    `branch=branch` with `branch="main"` at all four DraftOut/SpecOut
    construction sites passes every one of them. Only a workspace that is NOT on
    the default can tell a real read from a hardcoded string.
    """
    with pg_session_factory() as s:
        wid = _seed(s, _app, current_user)
        s.commit()

    r = client_authed_owner.post(f"/api/workspaces/{wid}/branches", json={"name": "v2"})
    assert r.status_code == 201, r.text
    r = client_authed_owner.post(f"/api/workspaces/{wid}/branches/switch", json={"name": "v2"})
    assert r.status_code == 200, r.text

    r = client_authed_owner.get(f"/api/workspaces/{wid}/draft")
    assert r.status_code == 200, r.text
    assert r.json()["branch"] == "v2"


def test_the_spec_reports_the_branch_it_is_actually_on(
    client_authed_owner, current_user, pg_session_factory, _app
):
    """The same gate for SpecOut, which has three construction sites of its own.

    The spec file is committed BEFORE the branch is created, so it exists on
    both branches and GET /spec answers 200 after the switch.
    """
    import subprocess

    with pg_session_factory() as s:
        wid = _seed(s, _app, current_user)
        s.commit()
    repo = workspace_repo_dir(_app.state.settings.workspaces_dir, wid)
    (repo / "proposal-spec.md").write_text("# Spec\n\nbody\n", encoding="utf-8")
    for argv in (["add", "proposal-spec.md"], ["commit", "-q", "-m", "spec"]):
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", *argv], cwd=repo, check=True
        )

    r = client_authed_owner.post(f"/api/workspaces/{wid}/branches", json={"name": "v2"})
    assert r.status_code == 201, r.text
    r = client_authed_owner.post(f"/api/workspaces/{wid}/branches/switch", json={"name": "v2"})
    assert r.status_code == 200, r.text

    r = client_authed_owner.get(f"/api/workspaces/{wid}/spec")
    assert r.status_code == 200, r.text
    assert r.json()["branch"] == "v2"


def test_a_save_from_the_previous_branch_is_refused_with_both_names(
    client_authed_owner, current_user, pg_session_factory, _app
):
    """The HTTP half of the cross-branch write guard.

    Every sha sent here is CORRECT for the target branch -- `v2` is made from
    HEAD, so section 1's bytes are identical on both and `base_commit_sha` is
    still the live head. That is deliberate: it means a 409 can only have come
    from the branch check. With valid shas and no branch field, save.py's stale
    guard is an AND that does not fire, and the write lands.

    `/draft` is never fetched, because the first read of an artifact flips
    collab on (enable-on-touch) and a collab-enabled artifact answers every
    git-first save with its own 409 -- which would make this test pass for
    entirely the wrong reason.
    """
    from grant_lab.proposal_assist.citations.gitio import current_head
    from grant_lab.proposal_assist.editing.sectionizer import split_sections

    with pg_session_factory() as s:
        wid = _seed(s, _app, current_user)
        s.commit()
    repo = workspace_repo_dir(_app.state.settings.workspaces_dir, wid)
    head = current_head(repo)
    section = split_sections((repo / "proposal.md").read_text())[0]

    assert (
        client_authed_owner.post(
            f"/api/workspaces/{wid}/branches", json={"name": "v2"}
        ).status_code
        == 201
    )
    assert (
        client_authed_owner.post(
            f"/api/workspaces/{wid}/branches/switch", json={"name": "v2"}
        ).status_code
        == 200
    )

    r = client_authed_owner.post(
        f"/api/workspaces/{wid}/sections/0",
        json={
            "content": "# Aims\n\nedited on main\n",
            "base_commit_sha": head,
            "base_section_sha": section.sha,
            "base_branch": "main",
        },
    )
    assert r.status_code == 409, r.text
    body = r.json()["error"]
    # Its OWN code, not the status-derived "conflict": a stale section is also a
    # 409, and the two need opposite answers in the UI.
    assert body["code"] == "branch_mismatch"
    # Both names, so the user can tell what happened to their text.
    assert "main" in body["message"]
    assert "v2" in body["message"]
    # And nothing was written.
    assert "edited on main" not in (repo / "proposal.md").read_text()


def test_artifact_snapshot_stays_on_the_recorded_branch_during_checkout(
    client_authed_owner, current_user, pg_session_factory, _app, monkeypatch
):
    """A read at the checkout-before-commit seam stays on the recorded branch.

    `switch_branch` sets `workspaces.current_branch` (branches.py step 8) and
    then runs `checkout_branch` (step 9) INSIDE the transaction; the commit
    happens afterwards, in `switch_branch_route` (web/branches.py:212). So
    between those two lines the git worktree already holds the TARGET branch's
    bytes while every other connection still reads the SOURCE branch's name.

    Rather than race a second HTTP request against the switch (nested TestClient
    portals deadlock), this peeks at the seam and uses the same DB-selected,
    immutable branch snapshot as the endpoint. HEAD itself is already on the
    target, so a worktree read would fail these assertions.
    """
    import subprocess

    from grant_lab.proposal_assist import branches as branches_mod
    from grant_lab.proposal_assist.citations.gitio import current_head, read_branch_file

    with pg_session_factory() as s:
        wid = _seed(s, _app, current_user)
        s.commit()
    repo = workspace_repo_dir(_app.state.settings.workspaces_dir, wid)
    text_v2 = (repo / "proposal.md").read_text()
    older = current_head(repo)

    # main moves on, so the two branches hold DIFFERENT bytes and a stale read
    # is visible rather than a coincidence.
    text_main = "# Aims\n\nmain moved on\n"
    (repo / "proposal.md").write_text(text_main, encoding="utf-8")
    for argv in (["add", "proposal.md"], ["commit", "-q", "-m", "main moves"]):
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", *argv], cwd=repo, check=True
        )
        main_head = current_head(repo)
    assert (repo / "proposal.md").read_text() != text_v2

    r = client_authed_owner.post(
        f"/api/workspaces/{wid}/branches", json={"name": "v2", "from_sha": older}
    )
    assert r.status_code == 201, r.text

    seen: dict[str, object] = {}
    real_checkout = branches_mod.checkout_branch

    def checkout_then_peek(repo_dir, name):
        real_checkout(repo_dir, name)
        # `_reconcile_head` (step 3) also checks out; only the step-9 call for
        # the target is the window under test.
        if name != "v2" or seen:
            return
        with pg_session_factory() as peek:
            row = peek.get(Workspace, wid)
            seen["branch"] = row.current_branch
            seen["head"], seen["text"] = read_branch_file(
                repo_dir, row.current_branch, "proposal.md"
            )

    monkeypatch.setattr(branches_mod, "checkout_branch", checkout_then_peek)

    r = client_authed_owner.post(f"/api/workspaces/{wid}/branches/switch", json={"name": "v2"})
    assert r.status_code == 200, r.text
    assert seen, "the step-9 checkout never ran; the seam moved"

    assert seen["branch"] == "main"
    assert seen["text"] == text_main
    assert seen["text"] != text_v2
    assert seen["head"] == main_head


def test_the_switch_response_carries_real_tips_not_an_empty_map(
    client_authed_owner, current_user, pg_session_factory, _app
):
    """Without this, the switch route can return `tips: {}` forever and every
    other test still passes -- they only ever read ["current"].

    The branches must DIVERGE, and each response is compared against the exact
    expected map rather than against the other. `switched == listed` is not a
    test: both routes call the same helper, so any corruption inside it agrees
    with itself. Measured 2026-08-31 -- collapsing every tip to a single sha
    left the earlier version of this test green, because `_seed` builds one
    commit and both branches pointed at it.
    """
    import subprocess

    from grant_lab.proposal_assist.citations.gitio import current_head

    with pg_session_factory() as s:
        wid = _seed(s, _app, current_user)
        s.commit()
    repo = workspace_repo_dir(_app.state.settings.workspaces_dir, wid)
    older = current_head(repo)

    (repo / "proposal.md").write_text("# Aims\n\nmore\n", encoding="utf-8")
    for argv in (["add", "proposal.md"], ["commit", "-q", "-m", "newer"]):
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", *argv], cwd=repo, check=True
        )
    newer = current_head(repo)
    assert older != newer

    r = client_authed_owner.post(
        f"/api/workspaces/{wid}/branches", json={"name": "v2", "from_sha": older}
    )
    assert r.status_code == 201, r.text

    switched = client_authed_owner.post(
        f"/api/workspaces/{wid}/branches/switch", json={"name": "v2"}
    )
    listed = client_authed_owner.get(f"/api/workspaces/{wid}/branches")

    assert switched.status_code == 200, switched.text
    assert switched.json()["tips"] == {"main": newer, "v2": older}
    assert listed.json()["tips"] == {"main": newer, "v2": older}
