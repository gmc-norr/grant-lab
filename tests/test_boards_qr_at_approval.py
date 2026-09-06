"""The QR is rendered at approval, in an authenticated request -- never on the
anonymous /b/<slug> path.

FIXTURES: `client_unauth` is defined at tests/proposal_assist/conftest.py:129,
package-scoped, and INVISIBLE here. `board`, `auth_headers`, `make_call`,
`publish` and `session` do not exist. The anonymous client is a plain
`with TestClient(app) as c:`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from grant_lab.auth.session_cookie import SessionSigner
from grant_lab.boards.qr import qr_for
from grant_lab.db.models import Board, BoardItem, Call, Source
from grant_lab.main import create_app

FUTURE = datetime.now(UTC) + timedelta(days=30)


def _setup(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    return create_app()


def _client_for(app, uid):
    signer: SessionSigner = app.state.session_signer
    c = TestClient(app)
    c.cookies.set("session", signer.make(user_id=uid))
    return c


def _make_board(pg_session_factory, *, profile_id, created_by, slug, is_published=True):
    with pg_session_factory() as s:
        b = Board(slug=slug, title="Corridor board", profile_id=profile_id,
                  created_by=created_by, is_published=is_published, orientation="landscape",
                  items_per_page=8, max_pages=3, page_seconds=30)
        s.add(b)
        s.commit()
        return b.id


def _make_call(pg_session_factory, *, external_id, url):
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(source_id=src.id, external_id=external_id, url=url,
                 title=f"Call {external_id}", deadline=FUTURE)
        s.add(c)
        s.commit()
        return c.id


def _approve_via_api(app, *, board_id, owner_id, call_ids):
    client = _client_for(app, owner_id)
    with client:
        return client.post(f"/api/boards/{board_id}/items", json={"call_ids": call_ids})


def test_the_public_route_encodes_nothing(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """GATE 1 -- the load-bearing one. Asserting the response is CORRECT would
    pass with the encode still on the hot path. Assert on cache MISSES."""
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="QR gate1 profile")
    slug = "g" * 16
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug=slug)
    cid = _make_call(pg_session_factory, external_id="qr-1", url="https://example.org/call/1")
    r = _approve_via_api(app, board_id=board_id, owner_id=owner_id, call_ids=[cid])
    assert r.status_code == 200, r.text

    qr_for.cache_clear()
    before = qr_for.cache_info().misses
    with TestClient(app) as c:
        rr = c.get(f"/api/public/boards/{slug}")
    assert rr.status_code == 200, rr.text
    assert qr_for.cache_info().misses == before, "the anonymous route ran the encoder"


def test_approval_stores_the_qr_and_its_source_url(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """GATE 2."""
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="QR gate2 profile")
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id,
                           slug="h" * 16)
    cid = _make_call(pg_session_factory, external_id="qr-2", url="https://example.org/call/1")
    r = _approve_via_api(app, board_id=board_id, owner_id=owner_id, call_ids=[cid])
    assert r.status_code == 200, r.text
    with pg_session_factory() as s:
        item = s.scalar(select(BoardItem).where(BoardItem.board_id == board_id,
                                                BoardItem.call_id == cid))
        assert item.qr_modules > 0
        assert item.qr_path
        assert item.qr_source_url == "https://example.org/call/1"


def test_an_overlong_url_stores_no_qr_and_the_card_still_renders(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """GATE 3. qr.py returns modules=0 above _MAX_URL_BYTES (=800, qr.py:124);
    the frontend already treats qr_modules === 0 as "no <svg>". The url below is
    920 bytes."""
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="QR gate3 profile")
    slug = "i" * 16
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug=slug)
    long_url = "https://example.org/" + "x" * 900
    cid = _make_call(pg_session_factory, external_id="qr-3", url=long_url)
    r = _approve_via_api(app, board_id=board_id, owner_id=owner_id, call_ids=[cid])
    assert r.status_code == 200, r.text
    with pg_session_factory() as s:
        item = s.scalar(select(BoardItem).where(BoardItem.call_id == cid))
        assert item.qr_modules == 0

    with TestClient(app) as c:
        body = c.get(f"/api/public/boards/{slug}").json()
    card = [i for i in body["items"] if i["url"].startswith("https://example.org/x")][0]
    assert card["qr_modules"] == 0
    assert card["title"]  # the card still renders


def test_a_url_changed_after_approval_serves_no_qr_rather_than_a_stale_one(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """GATE 4. A stored QR is derived data with the same exists-versus-current
    problem the freshness release just fixed for embeddings. A QR pointing
    somewhere the call no longer lives is worse than no QR -- it is on a wall."""
    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="QR gate4 profile")
    slug = "j" * 16
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id, slug=slug)
    cid = _make_call(pg_session_factory, external_id="qr-4", url="https://example.org/call/1")
    r = _approve_via_api(app, board_id=board_id, owner_id=owner_id, call_ids=[cid])
    assert r.status_code == 200, r.text

    with pg_session_factory() as s:
        s.get(Call, cid).url = "https://example.org/call/MOVED"
        s.commit()

    with TestClient(app) as c:
        body = c.get(f"/api/public/boards/{slug}").json()
    card = [i for i in body["items"] if i["url"].endswith("MOVED")][0]
    assert card["qr_modules"] == 0, "served a QR pointing at the old url"
    # Both halves of public.py's guard, not just one: a regression dropping only
    # the qr_path ternary would ship the stale path bytes in the anonymous JSON
    # with every other gate green. It would not render (BoardCard.tsx gates the
    # <svg> on qr_modules > 0), so this is defence in depth.
    assert card["qr_path"] == "", "served the stale QR path bytes"


def test_the_encode_does_not_run_while_the_approval_rows_are_locked(
    monkeypatch, pg_url, pg_session_factory, profile_factory
):
    """GATE 12 -- the lock-hold gate, and the load-bearing one for this change.

    Asserting the stored QR is CORRECT passes with the encode still inside the
    write transaction, which is the defect. The discriminating question is
    whether a SECOND connection can take the rows `approve_items` locks at the
    moment `qr_for` runs. `FOR UPDATE NOWAIT` raises instead of waiting, so it
    answers that without a timeout.

    Both rows are probed. The board row is what `_lock_board_row` takes; the
    PROFILE row is taken through `_board_for_edit` -> `_profile_for_edit` and
    matters more, because since that change it blocks 15 routes including 4
    GETs.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError, OperationalError

    from grant_lab.web.api import boards as boards_api

    app = _setup(monkeypatch, pg_url)
    pid, owner_id = profile_factory(name="QR lock-hold profile")
    board_id = _make_board(pg_session_factory, profile_id=pid, created_by=owner_id,
                           slug="l" * 16)
    cids = [
        _make_call(pg_session_factory, external_id=f"qr-lock-{i}",
                   url=f"https://example.org/lock/{i}")
        for i in range(3)
    ]

    held: list[str] = []
    real_qr_for = boards_api.qr_for

    def _locked(sql: str, params: dict) -> bool:
        try:
            with pg_session_factory() as probe_session:
                probe_session.execute(text(sql), params)
            return False
        except (OperationalError, DBAPIError):
            return True

    def probing_qr_for(url):
        if _locked("SELECT id FROM boards WHERE id = :b FOR UPDATE NOWAIT", {"b": board_id}):
            held.append("board")
        if _locked("SELECT id FROM profiles WHERE id = :p FOR UPDATE NOWAIT", {"p": pid}):
            held.append("profile")
        return real_qr_for(url)

    monkeypatch.setattr(boards_api, "qr_for", probing_qr_for)

    r = _approve_via_api(app, board_id=board_id, owner_id=owner_id, call_ids=cids)
    assert r.status_code == 200, r.text

    # Vacuity guard: a run where qr_for never fires proves nothing at all.
    with pg_session_factory() as s:
        stored = s.scalars(
            select(BoardItem.qr_modules).where(BoardItem.board_id == board_id)
        ).all()
    assert stored and all(m > 0 for m in stored), f"no QR was stored: {stored}"

    assert not held, (
        "the encode ran while approve_items held a row lock: "
        f"{sorted(set(held))} (x{len(held)} of 3 encodes)"
    )
