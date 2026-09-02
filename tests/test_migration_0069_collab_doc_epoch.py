"""0069 adds collab_artifact_state.doc_epoch: the counter a room shows every
client so a retained Y.Doc can prove which document it holds (spec
2026-09-02-collab-doc-epoch-design.md §3.1)."""

from sqlalchemy import inspect, text

from call_manager.db.models import CollabArtifactState, User, Workspace


def _ws(session):
    u = User(status="active", email="epoch-mig@ki.se")
    session.add(u)
    session.flush()
    ws = Workspace(title="W", owner_id=u.id, current_phase="active")
    session.add(ws)
    session.commit()
    return ws.id


def test_column_exists_not_null_bigint(migrated_engine):
    cols = {c["name"]: c for c in inspect(migrated_engine).get_columns("collab_artifact_state")}
    assert "doc_epoch" in cols, "0069 must add doc_epoch"
    assert cols["doc_epoch"]["nullable"] is False
    assert "BIGINT" in str(cols["doc_epoch"]["type"]).upper()


def test_a_row_inserted_without_the_column_reads_zero(pg_session_factory):
    # A pre-0069 row (or any writer that never names the column) is epoch 0:
    # the server default, not NULL, so the route's comparison never sees None.
    with pg_session_factory() as s:
        wid = _ws(s)
        s.execute(
            text(
                "INSERT INTO collab_artifact_state (workspace_id, artifact) VALUES (:wid, 'draft')"
            ),
            {"wid": wid},
        )
        s.commit()
        row = s.get(CollabArtifactState, (wid, "draft"))
        assert row.doc_epoch == 0


def test_orm_default_is_zero_too(pg_session_factory):
    with pg_session_factory() as s:
        wid = _ws(s)
        s.add(CollabArtifactState(workspace_id=wid, artifact="spec"))
        s.commit()
        assert s.get(CollabArtifactState, (wid, "spec")).doc_epoch == 0
