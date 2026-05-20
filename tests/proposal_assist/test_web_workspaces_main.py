"""Smoke for the workspace router skeleton. Subsequent tasks add real
behavior; here we just verify the router mounts + auth applies."""

import uuid

from tests.proposal_assist.conftest import make_workspace


def test_workspace_main_requires_auth(client_unauth, pg_session_factory):
    """Hitting GET /workspaces/<id> without a session redirects to login."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id
    response = client_unauth.get(
        f"/workspaces/{wsid}",
        follow_redirects=False,
        headers={"Accept": "text/html"},
    )
    assert response.status_code in (302, 303, 307)
    assert "/login" in response.headers.get("location", "")


def test_workspace_main_404_when_workspace_missing(client_authed_owner):
    """A non-existent workspace yields 404, not 500."""
    response = client_authed_owner.get(f"/workspaces/{uuid.uuid4()}")
    assert response.status_code == 404
