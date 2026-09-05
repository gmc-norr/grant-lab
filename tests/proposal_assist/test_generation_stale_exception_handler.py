"""WorkspaceGenerationStale must surface as 404, not 500 (Task 2 review, Finding 2).

Primitive B (`acquire_commit_mutex_at_generation`) raises an exception the old
primitive never could. Until the endgame's first conversion nothing in `src/`
could produce it on a request path, so nothing mapped it — and the moment two git
sites took Primitive B, the epoch race the fence exists to CATCH started landing
on `main.py`'s catch-all `Exception` handler as a sanitized 500.

Two things make that wrong rather than merely untidy:

* `ports/errors.py` says of this exception "It never reaches a user as a 5xx".
* A client cannot act on it. "The workspace you addressed is gone" is a 404 fact,
  and the same routes already answer 404 for it whenever the row is still deleted
  — flag-off from Primitive B's own `WorkspaceDeleted` (`fencing.py:166-167`),
  collab-ON from `RoomManager.operate`'s `WorkspaceRetired` (`collab/room.py:548,
  563, 577`). A 500 for the one variant where the row came BACK would be a
  different answer to the same question.

An earlier draft of this docstring said collab-ON "already answers exactly this
body" for the event this handler sees. It does not, and the distinction is worth
keeping straight because the last test in this file exercises exactly the case
where it fails:

* **Plain delete race** — parity holds, per the two paths named above.
* **Delete-and-RESTORE race** — the only race that can reach this handler (a
  still-deleted row is caught by `WorkspaceDeleted` first), and here flag-off is
  STRICTER than collab-ON. Restore clears the retired flag
  (`lifecycle.py:157`, `retired=False`) and leaves `soft_deleted_at` NULL, so
  `is_retired()` is False, the under-mutex liveness re-read passes, and collab-ON
  proceeds into the new epoch. Only the generation carried on the caller's stamp
  catches it. The 404 here is right on its own merits, not because it matches a
  collab-ON answer that does not exist.

One handler covers all 18 endgame sites, so this file is the place the mapping is
pinned as the remaining 16 convert.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from grant_lab.db.models import UserClaimAnnotation
from grant_lab.proposal_assist.drafting.locking import (
    DELETED_ERROR_MESSAGE,
    WorkspaceDeleted,
)
from grant_lab.proposal_assist.ports.errors import WorkspaceGenerationStale
from grant_lab.workspaces import restore_workspace_db, soft_delete_workspace_db
from tests.proposal_assist.test_api_annotations_insert import _seed_supported


def test_the_two_exceptions_are_siblings_so_one_handler_cannot_serve_both():
    """The premise of the registration, exactly as the WorkspaceRetired handler
    pins its own. Starlette resolves handlers by walking the MRO, so a sibling
    RuntimeError can never inherit WorkspaceDeleted's 404."""
    assert not issubclass(WorkspaceGenerationStale, WorkspaceDeleted)
    assert not issubclass(WorkspaceDeleted, WorkspaceGenerationStale)


def test_app_registers_a_workspace_generation_stale_handler(_app):
    """No converted route lists WorkspaceGenerationStale in its own except chain
    — losing this registration silently turns every epoch race into a 500."""
    assert WorkspaceGenerationStale in _app.exception_handlers


def test_a_raising_api_route_returns_the_same_404_body_as_workspace_deleted(_app):
    """Through the real ASGI stack. TestClient is used WITHOUT its context
    manager, as test_retired_exception_handler.py does: no lifespan, DB or
    scheduler is needed, because the handler reads nothing but the request path."""
    ws_id = uuid.uuid4()

    @_app.get("/api/_test/stale")
    async def _stale():            # pragma: no cover - body is the raise
        raise WorkspaceGenerationStale(ws_id, expected=0, actual=2)

    @_app.get("/api/_test/deleted")
    async def _deleted():          # pragma: no cover - body is the raise
        raise WorkspaceDeleted(str(ws_id))

    client = TestClient(_app, raise_server_exceptions=False)
    stale = client.get("/api/_test/stale")
    deleted = client.get("/api/_test/deleted")

    assert stale.status_code == 404                      # NOT 500
    # Byte-identical to WorkspaceDeleted, and to the WorkspaceRetired the
    # collab-ON fork raises on a PLAIN delete race: to a client these are one
    # event, and a response that differed would make the answer to "your
    # workspace is gone" depend on the collab flag. (On the delete+restore race
    # collab-ON raises nothing at all — see the module docstring; that gap is the
    # fence's remaining work, not a reason to answer this one differently.)
    assert stale.json() == deleted.json()
    assert stale.json()["error"]["code"] == "workspace_deleted"
    assert DELETED_ERROR_MESSAGE in stale.text


def test_the_stale_body_never_leaks_the_generations(_app):
    """`str(exc)` names the workspace id and both generations. The handler must
    answer with the shared message, not the exception text."""

    @_app.get("/api/_test/stale_leak")
    async def _stale():            # pragma: no cover - body is the raise
        raise WorkspaceGenerationStale(uuid.uuid4(), expected=0, actual=2)

    r = TestClient(_app, raise_server_exceptions=False).get("/api/_test/stale_leak")
    assert "generation" not in r.text
    assert r.json() == {"error": {"code": "workspace_deleted", "message": DELETED_ERROR_MESSAGE}}


def test_a_non_api_path_still_gets_a_plain_404(_app):
    """Parity with the WorkspaceDeleted / WorkspaceRetired non-/api branch."""

    @_app.get("/_test/stale")
    async def _stale():            # pragma: no cover - body is the raise
        raise WorkspaceGenerationStale(uuid.uuid4(), expected=0, actual=1)

    r = TestClient(_app, raise_server_exceptions=False).get("/_test/stale")
    assert r.status_code == 404
    assert r.json() == {"detail": DELETED_ERROR_MESSAGE}


def test_the_converted_insert_route_answers_404_when_the_epoch_moved(
    _app, current_user, pg_session_factory, tmp_path, monkeypatch
):
    """End-to-end on a CONVERTED route: POST .../annotations/{id}/insert.

    The staleness is produced the way production produces it — a real
    delete+restore COMMITTED from another connection in the window between the
    route's access check and the mutex. `annotations.py::_git` is
    `require_owner` -> `_annotation_or_404` -> `insert_from_annotation`, so
    wrapping `_annotation_or_404` occupies that exact seam; the route's own code
    is untouched and its `ws` object keeps the pre-transition generation the
    stamp is minted from, which is the whole point of the fence.

    Without the handler this is a 500. `raise_server_exceptions=False` is
    required for that to be observable rather than re-raised into the test.
    """
    _app.state.settings.workspaces_dir = str(tmp_path)
    _app.state.session_factory = pg_session_factory
    with pg_session_factory() as s:
        ws, ann_id, _repo = _seed_supported(s, tmp_path, current_user)
        s.commit()
        ws_id = ws.id

    import grant_lab.web.api.annotations as annotations_mod

    real = annotations_mod._annotation_or_404
    calls: list[int] = []

    def _bump_then_delegate(session, ws_obj, annotation_id):
        calls.append(1)
        # A committed lifecycle transition on another connection. Restore leaves
        # soft_deleted_at NULL again, so a liveness-only check would wave the
        # request through — only the generation catches it, and the fence's
        # WorkspaceDeleted branch is NOT what fires here.
        with pg_session_factory() as other:
            soft_delete_workspace_db(other, workspace_id=ws_id)
        with pg_session_factory() as other:
            restore_workspace_db(other, workspace_id=ws_id, admin_id=current_user.id)
        return real(session, ws_obj, annotation_id)

    monkeypatch.setattr(annotations_mod, "_annotation_or_404", _bump_then_delegate)

    client = TestClient(_app, raise_server_exceptions=False)
    client.__enter__()
    try:
        client.cookies.set("session", _app.state.session_signer.make(user_id=current_user.id))
        r = client.post(f"/api/workspaces/{ws_id}/annotations/{ann_id}/insert")
    finally:
        client.__exit__(None, None, None)

    assert calls == [1], "the seam never ran — this test would pass vacuously"
    assert r.status_code == 404, r.text                  # NOT 500
    assert r.json()["error"]["code"] == "workspace_deleted"
    # The fence raises BEFORE any effect, so the annotation is untouched.
    with pg_session_factory() as s:
        assert s.get(UserClaimAnnotation, ann_id).status == "open"
