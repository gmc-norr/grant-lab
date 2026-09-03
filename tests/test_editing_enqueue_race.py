"""Spec 2026-06-10 §1 — turn enqueue participates in the per-workspace commit mutex.

Race scenario: a user save holds the commit mutex; a concurrent turn enqueue must
block (not interleave) until the save's transaction releases the lock. This test
proves that submit_user_turn respects the mutex, making the save's "no
queued/running TurnJob" guard authoritative.

The CommitMutexTimeout path (acquire with a short timeout while mutex is held) is
already covered by test_editing_commit_mutex.py::test_commit_mutex_times_out_when_held
and is NOT duplicated here.
"""

import threading
import time

import pytest

from call_manager.db.models import TurnJob, User, Workspace
from call_manager.proposal_assist.drafting.locking import (
    acquire_commit_mutex,
)
from call_manager.proposal_assist.turns.submit import submit_user_turn


@pytest.fixture()
def _settings_env(monkeypatch, pg_url):
    """Populate the env vars Settings() requires (mirrors proposal_assist/conftest.py)."""
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")


def _ws(session):
    user = User(email="o@example.org", is_admin=False)
    session.add(user)
    session.flush()
    ws = Workspace(owner_id=user.id, title="t")
    session.add(ws)
    session.flush()
    return ws, user


def test_submit_blocks_while_commit_mutex_held(pg_session_factory, _settings_env):
    """submit_user_turn blocks until the workspace commit mutex is released.

    Sequence:
      1. s_hold acquires the commit mutex (simulating an in-flight user save).
      2. A background thread calls submit_user_turn — it must block on the mutex.
      3. After 0.3 s the main thread confirms the thread is still waiting (no
         "enqueued" in order).
      4. s_hold commits (releases the xact lock); the thread unblocks and
         completes the enqueue.
    """
    from call_manager.config import Settings

    settings = Settings()

    with pg_session_factory() as s_setup:
        ws, user = _ws(s_setup)
        s_setup.commit()
        ws_id, user_id = ws.id, user.id

    order: list[str] = []
    started = threading.Event()

    s_hold = pg_session_factory()
    acquire_commit_mutex(s_hold, ws_id)  # simulate an in-flight user save
    order.append("save-holding")

    def enqueue():
        with pg_session_factory() as s2:
            started.set()
            submit_user_turn(
                session=s2,
                workspace_id=ws_id,
                content_text="hi",
                submitted_by_user_id=user_id,
                settings=settings,
            )
            s2.commit()
            order.append("enqueued")

    t = threading.Thread(target=enqueue, daemon=True)
    t.start()
    started.wait(timeout=5)
    time.sleep(0.3)
    assert order == ["save-holding"], "enqueue must block while commit mutex is held"
    s_hold.commit()  # releases the xact advisory lock
    t.join(timeout=10)
    assert order == ["save-holding", "enqueued"]
    s_hold.close()

    with pg_session_factory() as s3:
        count = s3.query(TurnJob).filter_by(workspace_id=ws_id, status="queued").count()
    assert count == 1
