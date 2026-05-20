import uuid

import pytest

from call_manager.proposal_assist.drafting.locking import (
    LockNotAcquired,
    acquire_workspace_lock,
    release_workspace_lock,
    workspace_lock_key,
    workspace_lock_session,
)


def test_workspace_lock_key_is_stable_and_workspace_specific():
    """Same workspace → same key; different workspaces → different keys.
    Key is a bigint (Postgres' pg_advisory_lock signature)."""
    wsid1 = uuid.UUID("00000000-0000-0000-0000-000000000001")
    wsid2 = uuid.UUID("00000000-0000-0000-0000-000000000002")
    key1 = workspace_lock_key(wsid1)
    key2 = workspace_lock_key(wsid2)
    assert isinstance(key1, int)
    # Fits in a Postgres bigint (signed 64-bit).
    assert -(2**63) <= key1 < 2**63
    assert key1 == workspace_lock_key(wsid1)  # stable
    assert key1 != key2  # workspace-specific


def test_acquire_and_release_workspace_lock(pg_session_factory):
    """Acquire returns True when the lock is free; release returns True
    when we held it."""
    wsid = uuid.uuid4()
    with pg_session_factory() as s:
        assert acquire_workspace_lock(s, wsid) is True
        assert release_workspace_lock(s, wsid) is True


def test_acquire_blocks_second_session(pg_session_factory):
    """Two sessions can't hold the same workspace's lock at the same
    time. We use pg_try_advisory_lock semantics (non-blocking) — second
    attempt returns False, then succeeds after the first releases."""
    wsid = uuid.uuid4()
    with pg_session_factory() as s1, pg_session_factory() as s2:
        assert acquire_workspace_lock(s1, wsid) is True
        assert acquire_workspace_lock(s2, wsid) is False
        assert release_workspace_lock(s1, wsid) is True
        # Now s2 can acquire.
        assert acquire_workspace_lock(s2, wsid) is True
        assert release_workspace_lock(s2, wsid) is True


def test_different_workspaces_do_not_contend(pg_session_factory):
    wsid_a = uuid.uuid4()
    wsid_b = uuid.uuid4()
    with pg_session_factory() as s1, pg_session_factory() as s2:
        assert acquire_workspace_lock(s1, wsid_a) is True
        assert acquire_workspace_lock(s2, wsid_b) is True
        assert release_workspace_lock(s1, wsid_a) is True
        assert release_workspace_lock(s2, wsid_b) is True


def test_workspace_lock_session_context_manager_acquires_and_releases(
    pg_session_factory,
):
    """The context manager opens a session, acquires the lock, yields,
    then releases + closes. A second context manager call on the same
    workspace works because the first one released."""
    wsid = uuid.uuid4()
    with workspace_lock_session(pg_session_factory, wsid) as s:
        assert s is not None
        # Lock is held; a separate session can't acquire it.
        with pg_session_factory() as other:
            assert acquire_workspace_lock(other, wsid) is False

    # After exit, the lock is released — second context manager works.
    with workspace_lock_session(pg_session_factory, wsid) as s2:
        assert s2 is not None


def test_workspace_lock_session_releases_on_exception(pg_session_factory):
    """If the body raises, the context manager still releases the lock
    so a subsequent caller can acquire it."""
    wsid = uuid.uuid4()
    with (
        pytest.raises(RuntimeError, match="boom"),
        workspace_lock_session(pg_session_factory, wsid),
    ):
        raise RuntimeError("boom")

    # Lock should be released — next acquire succeeds.
    with pg_session_factory() as s:
        assert acquire_workspace_lock(s, wsid) is True
        release_workspace_lock(s, wsid)


def test_workspace_lock_session_raises_when_contended(pg_session_factory):
    """If the lock is already held by another session, the context manager
    raises LockNotAcquired immediately rather than blocking."""
    wsid = uuid.uuid4()
    with pg_session_factory() as holder:
        assert acquire_workspace_lock(holder, wsid) is True
        with (
            pytest.raises(LockNotAcquired) as exc_info,
            workspace_lock_session(pg_session_factory, wsid),
        ):
            pass
        assert exc_info.value.workspace_id == wsid
        release_workspace_lock(holder, wsid)
