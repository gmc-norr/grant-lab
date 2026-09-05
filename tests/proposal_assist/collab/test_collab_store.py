from datetime import UTC, datetime

from grant_lab.db.models import Workspace, YjsUpdate
from grant_lab.proposal_assist.collab import state, store
from tests.proposal_assist.conftest import make_user, make_workspace


def test_yjs_update_row_roundtrips_and_orders_by_id(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s, email="yjs-owner@umu.se")
        ws = make_workspace(s, owner=owner)
        s.add(
            YjsUpdate(workspace_id=ws.id, artifact="draft", actor_id=owner.id, update_bytes=b"\x01")
        )
        s.add(
            YjsUpdate(workspace_id=ws.id, artifact="draft", actor_id=owner.id, update_bytes=b"\x02")
        )
        s.commit()
        wid = ws.id
    with pg_session_factory() as s:
        from sqlalchemy import select

        rows = (
            s.execute(
                select(YjsUpdate)
                .where(YjsUpdate.workspace_id == wid, YjsUpdate.artifact == "draft")
                .order_by(YjsUpdate.id)
            )
            .scalars()
            .all()
        )
        assert [r.update_bytes for r in rows] == [b"\x01", b"\x02"]
        assert rows[0].id < rows[1].id
        assert rows[0].actor_id is not None


def test_append_and_load_updates_are_room_scoped_and_ordered(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s, email="store-owner@umu.se")
        ws = make_workspace(s, owner=owner)
        wid = ws.id
        uid = owner.id
        s.commit()

    with pg_session_factory() as s:
        store.append_update(s, workspace_id=wid, artifact="draft", actor_id=uid, update=b"a")
        store.append_update(s, workspace_id=wid, artifact="draft", actor_id=uid, update=b"b")
        store.append_update(s, workspace_id=wid, artifact="spec", actor_id=uid, update=b"z")
        s.commit()

    with pg_session_factory() as s:
        assert store.load_updates(s, workspace_id=wid, artifact="draft") == [b"a", b"b"]
        assert store.load_updates(s, workspace_id=wid, artifact="spec") == [b"z"]


def test_load_updates_empty_room_returns_empty_list(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s, email="empty-room@umu.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        assert store.load_updates(s, workspace_id=ws.id, artifact="draft") == []


def test_has_authored_updates_false_empty_false_seed_true_authored(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s, email="authored-owner@umu.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        wid, uid = ws.id, owner.id

    with pg_session_factory() as s:
        assert store.has_authored_updates(s, workspace_id=wid, artifact="draft") is False

    with pg_session_factory() as s:
        # unauthored hydration seed (actor_id IS NULL) — still untouched
        store.append_update(s, workspace_id=wid, artifact="draft", actor_id=None, update=b"seed")
        s.commit()
    with pg_session_factory() as s:
        assert store.has_authored_updates(s, workspace_id=wid, artifact="draft") is False
        # a different artifact/room is unaffected
        assert store.has_authored_updates(s, workspace_id=wid, artifact="spec") is False

    with pg_session_factory() as s:
        # a real, authored edit lands
        store.append_update(s, workspace_id=wid, artifact="draft", actor_id=uid, update=b"edit")
        s.commit()
    with pg_session_factory() as s:
        assert store.has_authored_updates(s, workspace_id=wid, artifact="draft") is True


def test_find_checkpoint_candidates_counts_rows_excludes_softdeleted(pg_session_factory):
    with pg_session_factory() as s:
        o = make_user(s, email="fcc-o@umu.se")
        ws1 = make_workspace(s, owner=o)   # lightly edited (1 uncheckpointed row)
        ws2 = make_workspace(s, owner=o)   # over threshold (3 uncheckpointed rows)
        ws3 = make_workspace(s, owner=o)   # over threshold BUT soft-deleted
        s.commit()
        w1, w2, w3 = ws1.id, ws2.id, ws3.id

    with pg_session_factory() as s:
        # ws1: one row, then checkpoint watermark advanced past it
        store.append_update(s, workspace_id=w1, artifact="draft", actor_id=None, update=b"\x01")
        s.commit()
        first_id = store.load_rows(s, workspace_id=w1, artifact="draft")[-1][0]
        state.advance_checkpoint(
            s, workspace_id=w1, artifact="draft", up_to_update_id=first_id, commit_sha="x"
        )
        s.commit()
        # ws2: 3 uncheckpointed rows (interleaves global ids, inflating ws1's id-gap)
        for b in (b"\x02", b"\x03", b"\x04"):
            store.append_update(s, workspace_id=w2, artifact="draft", actor_id=None, update=b)
        s.commit()
        # ws1: ONE more row — now its max_id - watermark gap is large (ws2 bumped the
        # global sequence) but its uncheckpointed COUNT is only 1.
        store.append_update(s, workspace_id=w1, artifact="draft", actor_id=None, update=b"\x05")
        s.commit()
        # ws3: 3 uncheckpointed rows, but soft-delete the workspace
        for b in (b"\x06", b"\x07", b"\x08"):
            store.append_update(s, workspace_id=w3, artifact="draft", actor_id=None, update=b)
        s.get(Workspace, w3).soft_deleted_at = datetime.now(UTC)
        s.commit()

    with pg_session_factory() as s:
        got = set(store.find_checkpoint_candidates(s, min_tail=3))

    assert got == {(w2, "draft")}   # ws1 excluded (count=1, NOT gap); ws3 excluded (soft-deleted)
