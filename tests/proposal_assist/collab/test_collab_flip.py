"""3b-5d.D4: enable-on-touch seam — sticky lazy enable with table hold-back."""

from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from call_manager.proposal_assist.collab import flip, state
from call_manager.proposal_assist.collab.flip import has_pipe_tables, maybe_auto_enable
from call_manager.proposal_assist.drafting.locking import acquire_commit_mutex
from tests.proposal_assist.conftest import make_user, make_workspace

# The flip's under-mutex re-read falls back to head_md when the file is absent, so
# these logic tests pass a path that does not exist (no repo on disk).
_NO_FILE = Path("/nonexistent-flip-test/proposal.md")


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
            s, workspace_id=wid, artifact="draft", head_md="## A\nx.\n", artifact_path=_NO_FILE
        )
        assert result is True
    with pg_session_factory() as s:  # committed by the hook — visible to a NEW session
        assert state.is_collab_enabled(s, workspace_id=wid, artifact="draft") is True
        # sticky repeat: no error, still True
        result = maybe_auto_enable(
            s, workspace_id=wid, artifact="draft", head_md="## A\nx.\n", artifact_path=_NO_FILE
        )
        assert result is True


def test_hold_back_on_pipe_tables(pg_session_factory):
    with pg_session_factory() as s:
        wid = _ws(s, "flip2@ki.se").id
        s.commit()
    with pg_session_factory() as s:
        md = "## T\n| a | b |\n|---|---|\n"
        assert (
            maybe_auto_enable(
                s, workspace_id=wid, artifact="draft", head_md=md, artifact_path=_NO_FILE
            )
            is False
        )
        assert state.is_collab_enabled(s, workspace_id=wid, artifact="draft") is False


def test_hold_back_when_fresh_disk_gained_a_table(pg_session_factory, tmp_path):
    """Item 2a: head_md (the pre-mutex read) is table-free, but a writer committed a
    table-adding edit before the flip re-read under the mutex — the FRESH bytes
    hold the flip back, so we never flip around the just-committed table."""
    with pg_session_factory() as s:
        wid = _ws(s, "flip2b@ki.se").id
        s.commit()
    artifact_path = tmp_path / "proposal.md"
    artifact_path.write_text("## T\n| a | b |\n|---|---|\n")  # fresh disk HAS a table
    with pg_session_factory() as s:
        assert (
            maybe_auto_enable(
                s,
                workspace_id=wid,
                artifact="draft",
                head_md="## T\ntable-free at read time.\n",
                artifact_path=artifact_path,
            )
            is False
        )
        assert state.is_collab_enabled(s, workspace_id=wid, artifact="draft") is False


def test_kill_switch_suppresses(pg_session_factory):
    state.set_kill_switch(True)
    with pg_session_factory() as s:
        wid = _ws(s, "flip3@ki.se").id
        s.commit()
        result = maybe_auto_enable(
            s, workspace_id=wid, artifact="draft", head_md="## A\nx.\n", artifact_path=_NO_FILE
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
        assert (
            maybe_auto_enable(
                s, workspace_id=wid, artifact="spec", head_md="## S\ny.\n", artifact_path=_NO_FILE
            )
            is True
        )
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
            s, workspace_id=wid, artifact="draft", head_md="## A\nx.\n", artifact_path=_NO_FILE
        )
        assert result is True


def test_mutex_held_by_writer_defers_flip(pg_session_factory, monkeypatch):
    """Item 2a: a git-first writer holds the commit mutex — the flip must NOT block
    the read indefinitely; it times out, defers (returns False), enables nothing,
    and leaves the caller's session usable. The next read retries."""
    monkeypatch.setattr(flip, "_FLIP_MUTEX_TIMEOUT_SECONDS", 1)  # keep the test fast
    with pg_session_factory() as s:
        wid = _ws(s, "flip6@ki.se").id
        s.commit()
    holder = pg_session_factory()
    try:
        acquire_commit_mutex(holder, wid)  # held until holder commits/rolls back
        with pg_session_factory() as s:
            result = maybe_auto_enable(
                s,
                workspace_id=wid,
                artifact="draft",
                head_md="## A\nx.\n",
                artifact_path=_NO_FILE,
            )
            assert result is False
            # session still usable after the aborted-mutex rollback
            assert state.is_collab_enabled(s, workspace_id=wid, artifact="draft") is False
    finally:
        holder.rollback()
        holder.close()
    with pg_session_factory() as s:  # nothing was persisted under contention
        assert state.is_collab_enabled(s, workspace_id=wid, artifact="draft") is False
