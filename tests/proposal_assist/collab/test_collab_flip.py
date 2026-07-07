"""3b-5d.D4: enable-on-touch seam — sticky lazy enable with table hold-back."""

import pytest
from sqlalchemy.exc import IntegrityError

from call_manager.proposal_assist.collab import state
from call_manager.proposal_assist.collab.flip import has_pipe_tables, maybe_auto_enable
from tests.proposal_assist.conftest import make_user, make_workspace


@pytest.fixture(autouse=True)
def _reset_kill_switch():
    yield
    state.set_kill_switch(False)


# --- has_pipe_tables ------------------------------------------------------ #

def test_detects_plain_pipe_table():
    assert has_pipe_tables("## T\n| a | b |\n|---|---|\n| 1 | 2 |\n") is True

def test_detects_leading_pipe_free_row():
    assert has_pipe_tables("intro\na | b | c\n") is True

def test_fenced_code_is_excluded():
    md = "## T\n```\n| a | b |\n|---|---|\n```\nprose.\n"
    assert has_pipe_tables(md) is False

def test_plain_prose_and_single_pipe_are_clean():
    assert has_pipe_tables("## T\neither/or, no tables here.\n") is False
    assert has_pipe_tables("a single | pipe is not a table row\n") is False


# --- maybe_auto_enable ----------------------------------------------------- #

def _ws(s, email):
    return make_workspace(s, owner=make_user(s, email=email))


def test_enables_once_sticky_and_commits(pg_session_factory):
    with pg_session_factory() as s:
        wid = _ws(s, "flip1@ki.se").id
        s.commit()
    with pg_session_factory() as s:
        result = maybe_auto_enable(
            s, workspace_id=wid, artifact="draft", head_md="## A\nx.\n"
        )
        assert result is True
    with pg_session_factory() as s:  # committed by the hook — visible to a NEW session
        assert state.is_collab_enabled(s, workspace_id=wid, artifact="draft") is True
        # sticky repeat: no error, still True
        result = maybe_auto_enable(
            s, workspace_id=wid, artifact="draft", head_md="## A\nx.\n"
        )
        assert result is True


def test_hold_back_on_pipe_tables(pg_session_factory):
    with pg_session_factory() as s:
        wid = _ws(s, "flip2@ki.se").id
        s.commit()
    with pg_session_factory() as s:
        md = "## T\n| a | b |\n|---|---|\n"
        assert maybe_auto_enable(s, workspace_id=wid, artifact="draft", head_md=md) is False
        assert state.is_collab_enabled(s, workspace_id=wid, artifact="draft") is False


def test_kill_switch_suppresses(pg_session_factory):
    state.set_kill_switch(True)
    with pg_session_factory() as s:
        wid = _ws(s, "flip3@ki.se").id
        s.commit()
        result = maybe_auto_enable(
            s, workspace_id=wid, artifact="draft", head_md="## A\nx.\n"
        )
        assert result is False
    state.set_kill_switch(False)
    with pg_session_factory() as s:  # nothing was persisted while suppressed
        assert state.is_collab_enabled(s, workspace_id=wid, artifact="draft") is False


def test_per_artifact_independence(pg_session_factory):
    with pg_session_factory() as s:
        wid = _ws(s, "flip4@ki.se").id
        s.commit()
    with pg_session_factory() as s:
        assert maybe_auto_enable(s, workspace_id=wid, artifact="spec", head_md="## S\ny.\n") is True
        assert state.is_collab_enabled(s, workspace_id=wid, artifact="draft") is False


def test_concurrent_first_read_race_reports_enabled(pg_session_factory, monkeypatch):
    """The loser of the insert race gets IntegrityError on commit -> rollback ->
    report True (the other request enabled it)."""
    with pg_session_factory() as s:
        wid = _ws(s, "flip5@ki.se").id
        s.commit()
    with pg_session_factory() as s:
        real_commit = s.commit
        calls = {"n": 0}

        def _racy_commit():
            if calls["n"] == 0:
                calls["n"] += 1
                raise IntegrityError("dup", None, Exception("pk"))
            real_commit()

        monkeypatch.setattr(s, "commit", _racy_commit)
        result = maybe_auto_enable(
            s, workspace_id=wid, artifact="draft", head_md="## A\nx.\n"
        )
        assert result is True
