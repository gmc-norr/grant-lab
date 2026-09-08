"""The note is projected on saved/hidden and NOWHERE else (§7 G1, G8, G9).

G1 asserts against the COMPILED SQL, not against a null in the response. A
populated control shows why: an inbox row has no decision row either way, so
`decision_note is None` is true with or without a needless subquery. A null
assertion cannot see the thing it is meant to test.
"""

from __future__ import annotations

import re

from grant_lab.db.models import CallDecision
from tests.test_api_calls_hide import _login, _seed_call, _setup

# Word boundaries: "call_decisions.note" must not be matched by a column whose
# name merely starts with it. `calls.embedding` matching `calls.embedding_model`
# is a trap this repo has hit once already.
_NOTE_COLUMN = re.compile(r"\bcall_decisions\.note\b")


def test_hidden_view_returns_the_note(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, _uid, pid = _login(app, pg_session_factory)
    with client:
        client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid})
        client.put(
            f"/api/calls/{cid}/note",
            params={"profile_id": pid},
            json={"note": "wrong field"},
        )
        r = client.get("/api/calls", params={"view": "hidden", "profile": pid})
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1, "the fixture must produce a row, or this gate is vacuous"
    assert items[0]["decision_note"] == "wrong field"


def test_saved_view_returns_the_note(monkeypatch, pg_url, pg_session_factory):
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, _uid, pid = _login(app, pg_session_factory)
    with client:
        client.post(f"/api/calls/{cid}/save", params={"profile_id": pid})
        client.put(f"/api/calls/{cid}/note", params={"profile_id": pid}, json={"note": "apply"})
        r = client.get("/api/calls", params={"view": "saved", "profile": pid})
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["decision_note"] == "apply"


def test_inbox_does_not_query_the_note_column(monkeypatch, pg_url, pg_session_factory):
    """G1. THE SQL, not the value. An undecided call has no decision row, so a
    null assertion here passes whether or not the column was selected."""
    _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, _uid, pid = _login(app, pg_session_factory)
    statements: list[str] = []
    from sqlalchemy import event

    # app.state.engine, NOT the session_factory's bind: the seeding fixtures
    # run on pg_session_factory's engine, a DIFFERENT Engine on the same
    # database, so only request traffic lands here. Precedent and the reason:
    # _statement_counter in tests/web/test_calls_list_query_count.py.
    engine = app.state.engine

    @event.listens_for(engine, "before_cursor_execute")
    def _capture(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    with client:
        r = client.get("/api/calls", params={"view": "inbox", "profile": pid})
    assert r.status_code == 200
    offenders = [s for s in statements if _NOTE_COLUMN.search(s)]
    assert offenders == [], offenders


def test_archive_does_not_query_the_note_column(monkeypatch, pg_url, pg_session_factory):
    """G1's other half. The spec says "absent for `inbox` AND `archive`"; only
    `inbox` was checked. `archive` is a SEPARATE branch of the same predicate --
    `decision_ordered` is `view in ("saved", "hidden")`, so `archive` reaches the
    add_columns() line by a different route than `inbox` does, and it is also the
    DEFAULT view, so it is the one an unparameterised GET lands on. A null
    assertion cannot see it either (`tests/test_api_calls.py` pins the null on
    this branch already), so this is the compiled SQL again."""
    _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, _uid, pid = _login(app, pg_session_factory)
    statements: list[str] = []
    from sqlalchemy import event

    # app.state.engine, NOT the session_factory's bind: the seeding fixtures
    # run on pg_session_factory's engine, a DIFFERENT Engine on the same
    # database, so only request traffic lands here. Precedent and the reason:
    # _statement_counter in tests/web/test_calls_list_query_count.py.
    engine = app.state.engine

    @event.listens_for(engine, "before_cursor_execute")
    def _capture(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    with client:
        r = client.get("/api/calls", params={"view": "archive", "profile": pid})
    assert r.status_code == 200
    offenders = [s for s in statements if _NOTE_COLUMN.search(s)]
    assert offenders == [], offenders


def test_hidden_view_DOES_query_the_note_column(monkeypatch, pg_url, pg_session_factory):
    """The control. Without this, deleting the column entirely would also make
    the inbox gate above pass -- the pair pins a DIFFERENCE, not an absence."""
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, _uid, pid = _login(app, pg_session_factory)
    statements: list[str] = []
    from sqlalchemy import event

    # app.state.engine, NOT the session_factory's bind: the seeding fixtures
    # run on pg_session_factory's engine, a DIFFERENT Engine on the same
    # database, so only request traffic lands here. Precedent and the reason:
    # _statement_counter in tests/web/test_calls_list_query_count.py.
    engine = app.state.engine

    @event.listens_for(engine, "before_cursor_execute")
    def _capture(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    with client:
        client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid})
        statements.clear()
        client.get("/api/calls", params={"view": "hidden", "profile": pid})
    # EXACTLY one, not `any`. `any` is satisfied by a second, separate
    # statement -- a bulk `SELECT call_id, note ... WHERE call_id IN (...)`
    # after the list query is still O(1) and still correct, so the N+1 gates
    # (which compare 1 row against 10) cannot see it either. But it is no
    # longer "one more column off the join that was already there", which is
    # this shape's entire justification over the spec rev-1 subselect. Measured
    # 2026-09-07: the count is 1 today.
    hits = [s for s in statements if _NOTE_COLUMN.search(s)]
    assert len(hits) == 1, hits


def test_another_users_note_is_invisible(monkeypatch, pg_url, pg_session_factory):
    """G9. Must reject a query keyed on (profile_id, call_id) without user_id --
    the natural mistake, because the golden-set miner IS keyed that way."""
    cid = _seed_call(pg_session_factory)
    app = _setup(monkeypatch, pg_url)
    client, uid, pid = _login(app, pg_session_factory)
    with client:
        client.post(f"/api/calls/{cid}/hide", params={"profile_id": pid})
        client.put(f"/api/calls/{cid}/note", params={"profile_id": pid}, json={"note": "mine"})
    # A SECOND user's decision on the SAME (profile, call), written straight
    # through the session: there is no endpoint for "act as someone else".
    from grant_lab.db.models import User

    with pg_session_factory() as s:
        other = User(status="active", email="colleague@umu.se")
        s.add(other)
        s.flush()
        s.add(
            CallDecision(
                user_id=other.id,
                profile_id=pid,
                call_id=cid,
                state="hidden",
                note="THEIRS",
                decided_by=other.id,
            )
        )
        s.commit()
    with client:
        r = client.get("/api/calls", params={"view": "hidden", "profile": pid})
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["decision_note"] == "mine"
    assert "THEIRS" not in r.text
