from grant_lab.proposal_assist.collab import state
from tests.proposal_assist.conftest import make_user, make_workspace


def test_flag_defaults_off_and_enable_is_idempotent(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="st-o@umu.se"))
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
        ws = make_workspace(s, owner=make_user(s, email="st-w@umu.se"))
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


def test_doc_epoch_is_zero_without_a_row(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="st-e0@umu.se"))
        s.commit()
        wid = ws.id
    with pg_session_factory() as s:
        assert state.get_doc_epoch(s, workspace_id=wid, artifact="draft") == 0


def test_reset_checkpoint_bumps_the_epoch_and_clears_the_watermark(pg_session_factory):
    # A truncated log is a new document (spec §3.1): every reset moves the
    # epoch by exactly one, and the watermark goes back to "never opened".
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="st-e1@umu.se"))
        state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        state.advance_checkpoint(
            s, workspace_id=ws.id, artifact="draft", up_to_update_id=7, commit_sha="abc"
        )
        s.commit()
        wid = ws.id
    with pg_session_factory() as s:
        state.reset_checkpoint(s, workspace_id=wid, artifact="draft")
        s.commit()
    with pg_session_factory() as s:
        assert state.get_doc_epoch(s, workspace_id=wid, artifact="draft") == 1
        assert state.get_checkpoint_watermark(s, workspace_id=wid, artifact="draft") == 0
        assert state.is_collab_enabled(s, workspace_id=wid, artifact="draft") is True
        state.reset_checkpoint(s, workspace_id=wid, artifact="draft")
        s.commit()
    with pg_session_factory() as s:
        assert state.get_doc_epoch(s, workspace_id=wid, artifact="draft") == 2


def test_reset_checkpoint_creates_a_missing_row_so_the_epoch_still_moves(pg_session_factory):
    # An artifact whose log is truncated before it ever had a state row (a
    # branch switch resets BOTH artifacts, enabled or not) must still leave
    # epoch 0 behind — otherwise a client from before the switch presents 0,
    # the room says 0, and the stale document is accepted.
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="st-e2@umu.se"))
        s.commit()
        wid = ws.id
    with pg_session_factory() as s:
        state.reset_checkpoint(s, workspace_id=wid, artifact="spec")
        s.commit()
    with pg_session_factory() as s:
        assert state.get_doc_epoch(s, workspace_id=wid, artifact="spec") == 1
        # created but NOT enabled: reset never flips the flag
        assert state.is_collab_enabled(s, workspace_id=wid, artifact="spec") is False
        # per-artifact: draft is untouched
        assert state.get_doc_epoch(s, workspace_id=wid, artifact="draft") == 0
