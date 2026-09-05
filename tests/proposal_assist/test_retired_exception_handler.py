"""WorkspaceRetired must surface as 404, not 500 (post-B-retire review fix).

B-retire gave RoomManager.acquire/operate a new raise. `operate` raises from
__aenter__, i.e. BEFORE the `try` each kernel writer opens inside its `async
with`, so the exception lands in the calling route's except chain — which lists
only cite/git/mutex errors. WorkspaceRetired and WorkspaceDeleted are SIBLING
RuntimeErrors (neither subclasses the other), so Starlette's MRO walk could not
reuse the WorkspaceDeleted handler either, and the catch-all Exception handler
turned a routine delete race into a 500 where it used to be a clean 404.
"""

import uuid

from fastapi.testclient import TestClient

from grant_lab.proposal_assist.drafting.contract import WorkspaceRetired
from grant_lab.proposal_assist.drafting.locking import DELETED_ERROR_MESSAGE, WorkspaceDeleted


def test_the_two_exceptions_are_siblings_so_one_handler_cannot_serve_both():
    """The premise of the fix. If someone later makes WorkspaceRetired subclass
    WorkspaceDeleted this test fails loudly and the extra registration can go."""
    assert not issubclass(WorkspaceRetired, WorkspaceDeleted)
    assert not issubclass(WorkspaceDeleted, WorkspaceRetired)


def test_app_registers_a_workspace_retired_handler(_app):
    """Mirrors test_commit_mutex_live's WorkspaceDeleted assertion: no writer route
    lists WorkspaceRetired in its own except chain, so losing this registration
    silently turns every delete race back into a 500."""
    assert WorkspaceRetired in _app.exception_handlers


def test_a_raising_api_route_returns_the_same_404_body_as_workspace_deleted(_app):
    """End-to-end through the real ASGI stack. Routes are mounted on the per-test
    create_app() instance (the _app fixture is function-scoped), and TestClient is
    used WITHOUT its context manager so no lifespan/DB/scheduler is needed — the
    handler reads nothing but the request path."""
    ws_id = uuid.uuid4()

    @_app.get("/api/_test/retired")
    async def _retired():          # pragma: no cover - body is the raise
        raise WorkspaceRetired(ws_id)

    @_app.get("/api/_test/deleted")
    async def _deleted():          # pragma: no cover - body is the raise
        raise WorkspaceDeleted(str(ws_id))

    client = TestClient(_app, raise_server_exceptions=False)
    retired = client.get("/api/_test/retired")
    deleted = client.get("/api/_test/deleted")

    assert retired.status_code == 404          # NOT 500
    # Byte-identical to the pre-B-retire outcome: the same race used to be refused
    # by the live commit mutex as WorkspaceDeleted, and a client cannot tell the
    # two apart (nor should it — the workspace is gone either way).
    assert retired.json() == deleted.json()
    assert retired.json()["error"]["code"] == "workspace_deleted"
    assert DELETED_ERROR_MESSAGE in retired.text


def test_a_non_api_path_still_gets_a_plain_404(_app):
    """Parity with the WorkspaceDeleted handler's non-/api branch (no envelope)."""

    @_app.get("/_test/retired")
    async def _retired():          # pragma: no cover - body is the raise
        raise WorkspaceRetired(uuid.uuid4())

    r = TestClient(_app, raise_server_exceptions=False).get("/_test/retired")
    assert r.status_code == 404
    assert r.json() == {"detail": DELETED_ERROR_MESSAGE}
