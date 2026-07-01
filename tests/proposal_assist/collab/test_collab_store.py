from call_manager.db.models import YjsUpdate
from call_manager.proposal_assist.collab import store
from tests.proposal_assist.conftest import make_user, make_workspace


def test_yjs_update_row_roundtrips_and_orders_by_id(pg_session_factory):
    with pg_session_factory() as s:
        owner = make_user(s, email="yjs-owner@ki.se")
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
        owner = make_user(s, email="store-owner@ki.se")
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
        owner = make_user(s, email="empty-room@ki.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        assert store.load_updates(s, workspace_id=ws.id, artifact="draft") == []
