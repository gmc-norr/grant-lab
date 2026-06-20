"""B1 / Task 12: post_turn user-facing error branches surface as JSON
HTTPException (``{"detail": ...}``), not HTMLResponse.

Task 11 made the global exception handler return JSON ``{"detail": ...}``
for htmx partial requests regardless of Accept, so an HTTPException raised
in post_turn reaches app.js's ``htmx:responseError`` handler, which renders
``detail`` inline via ``textContent``. These tests pin that the error
branches now produce JSON (parseable ``resp.json()["detail"]``) rather than
an HTML ``<p>...</p>`` body.
"""

from __future__ import annotations


def test_disclosure_gate_returns_json_403(client_authed_owner, pg_session_factory, current_user):
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, current_phase="active")  # NO ack_disclosure
        s.commit()
        wsid = ws.id
    resp = client_authed_owner.post(
        f"/workspaces/{wsid}/turns",
        data={"content": "hi"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 403
    assert "disclosure" in resp.json()["detail"].lower()


def test_too_many_inclusions_returns_json_400(
    client_authed_owner, pg_session_factory, current_user
):
    import uuid as _uuid

    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, current_phase="active", ack_disclosure=True)
        s.commit()
        wsid = ws.id
    # dict form with a list value for the repeated reference_ids field —
    # httpx encodes each list element as its own form pair. (A list-of-
    # tuples ``data=`` loses the single ``content`` pair under httpx's
    # urlencoder, yielding a 422 before the route body runs.)
    data = {"content": "hi", "reference_ids": [str(_uuid.uuid4()) for _ in range(6)]}
    resp = client_authed_owner.post(
        f"/workspaces/{wsid}/turns",
        data=data,
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 400
    assert "too many inclusions" in resp.json()["detail"].lower()
