from grant_lab.proposal_assist.collab import store
from tests.proposal_assist.conftest import make_user, make_workspace


def test_append_update_commit_false_defers_to_caller(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="stg-o@umu.se"))
        s.commit()
        wid = ws.id
    # stage but roll back -> nothing persisted
    with pg_session_factory() as s:
        store.append_update(
            s, workspace_id=wid, artifact="draft", actor_id=None, update=b"\x01", commit=False
        )
        s.rollback()
    with pg_session_factory() as s:
        assert store.load_updates(s, workspace_id=wid, artifact="draft") == []
    # stage then commit -> persisted; and the same-session read sees its own staged row
    with pg_session_factory() as s:
        store.append_update(
            s, workspace_id=wid, artifact="draft", actor_id=None, update=b"\x02", commit=False
        )
        # own uncommitted write
        assert store.load_updates(s, workspace_id=wid, artifact="draft") == [b"\x02"]
        s.commit()
    with pg_session_factory() as s:
        assert store.load_updates(s, workspace_id=wid, artifact="draft") == [b"\x02"]
