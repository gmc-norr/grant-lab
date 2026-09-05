"""3b-5d.D4: enable-on-touch seam — sticky lazy enable."""

import pytest
from sqlalchemy.exc import IntegrityError

from grant_lab.proposal_assist.collab import flip, state
from grant_lab.proposal_assist.collab.flip import maybe_auto_enable
from grant_lab.proposal_assist.drafting.locking import acquire_commit_mutex
from tests.proposal_assist.conftest import make_user, make_workspace, stamp_for


@pytest.fixture(autouse=True)
def _reset_kill_switch():
    yield
    state.set_kill_switch(False)


# --- maybe_auto_enable ----------------------------------------------------- #

def _ws(s, email):
    return make_workspace(s, owner=make_user(s, email=email))


def test_enables_once_sticky_and_commits(pg_session_factory):
    with pg_session_factory() as s:
        wid = _ws(s, "flip1@umu.se").id
        s.commit()
    with pg_session_factory() as s:
        result = maybe_auto_enable(s, stamp=stamp_for(s, wid), workspace_id=wid, artifact="draft")
        assert result is True
    with pg_session_factory() as s:  # committed by the hook — visible to a NEW session
        assert state.is_collab_enabled(s, workspace_id=wid, artifact="draft") is True
        # sticky repeat: no error, still True
        result = maybe_auto_enable(s, stamp=stamp_for(s, wid), workspace_id=wid, artifact="draft")
        assert result is True


def test_kill_switch_suppresses(pg_session_factory):
    state.set_kill_switch(True)
    with pg_session_factory() as s:
        wid = _ws(s, "flip3@umu.se").id
        s.commit()
        result = maybe_auto_enable(s, stamp=stamp_for(s, wid), workspace_id=wid, artifact="draft")
        assert result is False
    state.set_kill_switch(False)
    with pg_session_factory() as s:  # nothing was persisted while suppressed
        assert state.is_collab_enabled(s, workspace_id=wid, artifact="draft") is False


def test_per_artifact_independence(pg_session_factory):
    with pg_session_factory() as s:
        wid = _ws(s, "flip4@umu.se").id
        s.commit()
    with pg_session_factory() as s:
        assert (
            maybe_auto_enable(s, stamp=stamp_for(s, wid), workspace_id=wid, artifact="spec")
            is True
        )
        assert state.is_collab_enabled(s, workspace_id=wid, artifact="draft") is False


def test_concurrent_first_read_race_reports_enabled(pg_session_factory, monkeypatch):
    """The loser of the insert race gets IntegrityError on commit -> rollback ->
    report True (the other request enabled it)."""
    with pg_session_factory() as s:
        wid = _ws(s, "flip5@umu.se").id
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
        result = maybe_auto_enable(s, stamp=stamp_for(s, wid), workspace_id=wid, artifact="draft")
        assert result is True


def test_mutex_held_by_writer_defers_flip(pg_session_factory, monkeypatch):
    """Item 2a: a git-first writer holds the commit mutex — the flip must NOT block
    the read indefinitely; it times out, defers (returns False), enables nothing,
    and leaves the caller's session usable. The next read retries."""
    monkeypatch.setattr(flip, "_FLIP_MUTEX_TIMEOUT_SECONDS", 1)  # keep the test fast
    with pg_session_factory() as s:
        wid = _ws(s, "flip6@umu.se").id
        s.commit()
    holder = pg_session_factory()
    try:
        acquire_commit_mutex(holder, wid)  # held until holder commits/rolls back
        with pg_session_factory() as s:
            result = maybe_auto_enable(
                s, stamp=stamp_for(s, wid), workspace_id=wid, artifact="draft"
            )
            assert result is False
            # session still usable after the aborted-mutex rollback
            assert state.is_collab_enabled(s, workspace_id=wid, artifact="draft") is False
    finally:
        holder.rollback()
        holder.close()
    with pg_session_factory() as s:  # nothing was persisted under contention
        assert state.is_collab_enabled(s, workspace_id=wid, artifact="draft") is False


def test_table_doc_enables_now(pg_session_factory):
    """The D4 hold-back is gone: a pipe-table artifact enables like any other
    (the bridge round-trips GFM tables losslessly — tables slice §G)."""
    with pg_session_factory() as s:
        wid = _ws(s, "flip7@umu.se").id
        s.commit()
    with pg_session_factory() as s:
        assert (
            maybe_auto_enable(s, stamp=stamp_for(s, wid), workspace_id=wid, artifact="draft")
            is True
        )
        assert state.is_collab_enabled(s, workspace_id=wid, artifact="draft") is True
