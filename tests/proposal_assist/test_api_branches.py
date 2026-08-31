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

from call_manager.db.models import Branch, Workspace
from call_manager.proposal_assist.paths import workspace_repo_dir
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
    with pg_session_factory() as s:
        wid = _seed(s, _app, current_user)
        s.commit()
    r = client_authed_owner.get(f"/api/workspaces/{wid}/branches")
    assert r.status_code == 200
    assert r.json() == {"branches": ["main"], "current": "main"}


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
        stranger = make_user(s, email="stranger-branch@ki.se")
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
    from call_manager.db.models import WorkspaceViewer

    with pg_session_factory() as s:
        other_owner = make_user(s, email="branch-owner@ki.se")
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
