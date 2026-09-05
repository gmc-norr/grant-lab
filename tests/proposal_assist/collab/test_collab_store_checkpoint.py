from grant_lab.proposal_assist.collab import store
from tests.proposal_assist.conftest import make_user, make_workspace


def test_load_rows_returns_ordered_id_actor_bytes(pg_session_factory):
    with pg_session_factory() as s:
        o = make_user(s, email="lr-o@umu.se")
        u2 = make_user(s, email="lr-2@umu.se")
        ws = make_workspace(s, owner=o)
        s.commit()
        wid, oid, u2id = ws.id, o.id, u2.id
    with pg_session_factory() as s:
        assert store.load_rows(s, workspace_id=wid, artifact="draft") == []
        # seed row (actor_id=None)
        store.append_update(s, workspace_id=wid, artifact="draft", actor_id=None, update=b"\x01")
        store.append_update(s, workspace_id=wid, artifact="draft", actor_id=oid, update=b"\x02")
        store.append_update(s, workspace_id=wid, artifact="draft", actor_id=u2id, update=b"\x03")
        # a different artifact must not leak into the snapshot:
        store.append_update(s, workspace_id=wid, artifact="spec", actor_id=oid, update=b"\xff")
        s.commit()
        rows = store.load_rows(s, workspace_id=wid, artifact="draft")
        assert [r[2] for r in rows] == [b"\x01", b"\x02", b"\x03"]  # ordered by id, draft only
        assert [r[1] for r in rows] == [None, oid, u2id]  # actor_id (nullable) preserved
        ids = [r[0] for r in rows]
        assert ids == sorted(ids)                                        # monotonic ids
        # callers derive max_id + authors from THIS snapshot (no second query):
        max_id = rows[-1][0]
        watermark = ids[0]                      # the seed row's id is 'already checkpointed'
        authors = sorted({a for (i, a, _) in rows if i > watermark and a is not None})
        assert max_id == ids[-1]
        assert authors == sorted([oid, u2id])
