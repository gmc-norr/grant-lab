from call_manager.proposal_assist.collab import state
from tests.proposal_assist.conftest import make_user, make_workspace


def test_flag_defaults_off_and_enable_is_idempotent(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="st-o@ki.se"))
        s.commit()
        wid = ws.id
    with pg_session_factory() as s:
        assert state.is_collab_enabled(s, workspace_id=wid, artifact="draft") is False
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        state.enable_collab(s, workspace_id=wid, artifact="draft")  # idempotent
        s.commit()
    with pg_session_factory() as s:
        assert state.is_collab_enabled(s, workspace_id=wid, artifact="draft") is True
        # per-artifact: enabling "draft" must not affect "spec"
        assert state.is_collab_enabled(s, workspace_id=wid, artifact="spec") is False


def test_watermark_roundtrips(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="st-w@ki.se"))
        s.commit()
        wid = ws.id
    with pg_session_factory() as s:
        assert state.get_checkpoint_watermark(s, workspace_id=wid, artifact="draft") == 0
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        state.advance_checkpoint(
            s, workspace_id=wid, artifact="draft", up_to_update_id=42, commit_sha="abc123"
        )
        s.commit()
    with pg_session_factory() as s:
        assert state.get_checkpoint_watermark(s, workspace_id=wid, artifact="draft") == 42
