from fastapi.testclient import TestClient

from grant_lab.db.models import Board
from tests.web.test_api_public_board import _approve, _make_board, _make_call, _new_user, _setup


def test_draft_preview_is_owner_only_and_does_not_publish(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    app = _setup(monkeypatch, pg_url)
    profile_id, owner_id = profile_factory()
    reader_id = _new_user(pg_session_factory, "reader@umu.se")
    slug = "preview-private-board"
    board_id = _make_board(
        pg_session_factory,
        profile_id=profile_id,
        created_by=owner_id,
        slug=slug,
        is_published=False,
    )
    call_id = _make_call(pg_session_factory, external_id="preview-call", title="Draft card")
    _approve(pg_session_factory, board_id=board_id, call_id=call_id, approved_by=owner_id)
    with TestClient(app) as client:
        assert client.get(f"/api/boards/{board_id}/preview").status_code == 401
        client.cookies.set("session", app.state.session_signer.make(user_id=reader_id))
        assert client.get(f"/api/boards/{board_id}/preview").status_code == 403
        client.cookies.clear()
        client.cookies.set("session", app.state.session_signer.make(user_id=owner_id))
        preview = client.get(f"/api/boards/{board_id}/preview")
        assert preview.status_code == 200
        assert preview.headers["cache-control"] == "private, no-store"
        assert preview.json()["items"][0]["title"] == "Draft card"
        assert preview.json()["items"][0]["qr_path"]
        assert client.get(f"/api/public/boards/{slug}").status_code == 404
        with pg_session_factory() as session:
            board = session.get(Board, board_id)
            assert not board.is_published
            board.is_published = True
            session.commit()
        published = client.get(f"/api/public/boards/{slug}").json()
        assert published["items"] == preview.json()["items"]
