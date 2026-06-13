import asyncio

import pytest
from sqlalchemy import select

from call_manager.db.models import ChatTurn, TurnJob, WorkspaceEvent
from call_manager.proposal_assist.turns.caps import TokenCapExceeded
from call_manager.proposal_assist.turns.submit import submit_user_turn
from tests.proposal_assist.conftest import (
    make_chat_turn,
    make_user,
    make_workspace,
)


def test_submit_user_turn_writes_chat_turn_and_turn_job(
    pg_session_factory,
    _settings_env,
):
    from call_manager.config import Settings

    settings = Settings()
    with pg_session_factory() as s:
        user = make_user(s)
        ws = make_workspace(s, owner=user)
        s.commit()
        uid, wsid = user.id, ws.id

    with pg_session_factory() as s:
        job = submit_user_turn(
            session=s,
            workspace_id=wsid,
            content_text="Hello agent.",
            submitted_by_user_id=uid,
            settings=settings,
        )
        s.commit()
        job_id = job.id

    with pg_session_factory() as s:
        turn_rows = list(s.scalars(select(ChatTurn).where(ChatTurn.workspace_id == wsid)))
        job_row = s.scalar(select(TurnJob).where(TurnJob.id == job_id))
        events = list(
            s.scalars(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.workspace_id == wsid,
                    WorkspaceEvent.kind == "cloud_turn_sent",
                )
            )
        )

    assert len(turn_rows) == 1
    assert turn_rows[0].role == "user"
    assert turn_rows[0].content == {"type": "text", "text": "Hello agent."}
    assert job_row.status == "queued"
    assert job_row.user_turn_id == turn_rows[0].id
    assert job_row.submitted_by_user_id == uid
    assert job_row.queued_at is not None
    # Audit event written at submit-time so it survives a crash before finalize.
    assert len(events) == 1
    assert events[0].actor_id == uid
    assert events[0].payload["turn_job_id"] == str(job_id)
    assert events[0].payload["phase"] == "queued"


def test_submit_user_turn_raises_when_input_cap_exceeded(
    pg_session_factory,
    _settings_env,
    monkeypatch,
):
    monkeypatch.setenv("DAILY_TOKEN_CAP_INPUT", "100")
    from call_manager.config import Settings

    settings = Settings()
    with pg_session_factory() as s:
        user = make_user(s)
        ws = make_workspace(s, owner=user)
        make_chat_turn(
            s, ws, role="assistant", content={"type": "text", "text": "x"}, tokens_in=200
        )
        s.commit()
        uid, wsid = user.id, ws.id

    with pg_session_factory() as s:
        with pytest.raises(TokenCapExceeded) as exc_info:
            submit_user_turn(
                session=s,
                workspace_id=wsid,
                content_text="hi",
                submitted_by_user_id=uid,
                settings=settings,
            )
        s.rollback()

    assert exc_info.value.reason == "input_cap_exceeded"

    # No rows written.
    with pg_session_factory() as s:
        user_rows = list(
            s.scalars(
                select(ChatTurn).where(
                    ChatTurn.workspace_id == wsid,
                    ChatTurn.role == "user",
                )
            )
        )
        jobs = list(s.scalars(select(TurnJob).where(TurnJob.workspace_id == wsid)))
        events = list(
            s.scalars(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.workspace_id == wsid,
                    WorkspaceEvent.kind == "cloud_turn_sent",
                )
            )
        )
    assert user_rows == []
    assert jobs == []
    assert events == []  # audit row also rolled back


def test_submit_user_turn_accepts_null_submitter_for_system_initiated(
    pg_session_factory,
    _settings_env,
):
    """System-initiated turns (future auto-summarization, recovery)
    legitimately have no user actor — pass None."""
    from call_manager.config import Settings

    settings = Settings()
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id

    with pg_session_factory() as s:
        job = submit_user_turn(
            session=s,
            workspace_id=wsid,
            content_text="system message",
            submitted_by_user_id=None,
            settings=settings,
        )
        s.commit()
        job_id = job.id

    with pg_session_factory() as s:
        job_row = s.scalar(select(TurnJob).where(TurnJob.id == job_id))
        events = list(
            s.scalars(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.workspace_id == wsid,
                    WorkspaceEvent.kind == "cloud_turn_sent",
                )
            )
        )
    assert job_row.submitted_by_user_id is None
    assert events[0].actor_id is None


@pytest.mark.asyncio
async def test_concurrent_submits_cannot_overshoot_cap(monkeypatch, pg_url, pg_session_factory):
    """Two concurrent submits, each individually fine, must not both
    succeed if together they'd exceed the daily cap. The cap check +
    insert must serialize via row-level lock on workspaces.

    Scenario: seed=60 tokens, cap=100, each new turn contributes tokens_in=50.
    Thread 1 reads sum=60 (below cap), inserts its turn, commits → sum=110.
    Thread 2 (with FOR UPDATE) reads sum=110 after thread 1 commits → capped.
    Thread 2 (without FOR UPDATE) would read sum=60, also succeed — TOCTOU:
    both turn_jobs queued, 60+50+50=160 tokens billed, cap overshot by 60.

    `tokens_in=50` is passed explicitly to simulate a caller (e.g. B7
    attachment turns) that pre-counts message tokens at submit time so the
    cap sum is immediately accurate after commit.

    Note: today's web caller does NOT pass `tokens_in`, so this test
    exercises the B7-shape caller. For text-only B6 traffic the lock is
    defense-in-depth — both submits still pass because user-turn
    `tokens_in` is NULL until the processor fills it in from the LLM
    response."""
    from concurrent.futures import ThreadPoolExecutor
    from datetime import UTC, datetime, timedelta

    from call_manager.config import Settings
    from call_manager.db.models import ChatTurn
    from call_manager.proposal_assist.turns.caps import TokenCapExceeded
    from call_manager.proposal_assist.turns.submit import submit_user_turn
    from tests.proposal_assist.conftest import make_user, make_workspace

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("DAILY_TOKEN_CAP_INPUT", "100")
    monkeypatch.setenv("DAILY_TOKEN_CAP_OUTPUT", "1000000")
    settings = Settings()

    with pg_session_factory() as s:
        owner = make_user(s, email="concurrent-test-owner@ki.se")
        s.commit()
        ws = make_workspace(s, owner=owner)
        s.commit()
        # Seed: 60 input tokens used today (from a completed assistant turn).
        # Cap is 100. A single new turn with tokens_in=50 would read 60,
        # pass (60 < 100), insert, commit → sum becomes 110.
        # A second concurrent turn that reads BEFORE the first commits
        # also reads 60, also passes — TOCTOU. With FOR UPDATE, the second
        # waits for the first to commit, then reads 110 and is capped.
        s.add(
            ChatTurn(
                workspace_id=ws.id,
                role="assistant",
                content={"type": "text", "text": "x"},
                tokens_in=60,
                created_at=datetime.now(UTC) - timedelta(minutes=5),
            )
        )
        s.commit()
        wsid = ws.id
        owner_id = owner.id

    def attempt():
        with pg_session_factory() as session:
            try:
                submit_user_turn(
                    session=session,
                    workspace_id=wsid,
                    content_text="hi",
                    submitted_by_user_id=owner_id,
                    settings=settings,
                    tokens_in=50,
                )
                session.commit()
                return "ok"
            except TokenCapExceeded:
                session.rollback()
                return "capped"

    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=2) as ex:
        results = await asyncio.gather(
            loop.run_in_executor(ex, attempt),
            loop.run_in_executor(ex, attempt),
        )

    assert sorted(results) == ["capped", "ok"], f"expected one ok and one capped, got {results}"


def test_submit_rejected_on_soft_deleted_workspace(pg_session_factory, _settings_env):
    """A submit that races a soft_delete (workspace already soft-deleted by the
    time submit acquires the FOR UPDATE lock) must be rejected — no TurnJob,
    no cloud_turn_sent — so the drain can never orphan a queued turn."""
    from datetime import UTC, datetime

    import pytest
    from sqlalchemy import select

    from call_manager.config import Settings
    from call_manager.db.models import TurnJob, WorkspaceEvent
    from call_manager.proposal_assist.turns.submit import submit_user_turn
    from tests.proposal_assist.conftest import make_user, make_workspace

    settings = Settings()

    with pg_session_factory() as s:
        owner = make_user(s)
        ws = make_workspace(s, owner=owner)
        ws.soft_deleted_at = datetime.now(UTC)  # already soft-deleted
        s.commit()
        wsid = ws.id

    with pg_session_factory() as s:
        with pytest.raises(ValueError):
            submit_user_turn(
                session=s,
                workspace_id=wsid,
                content_text="hi",
                submitted_by_user_id=owner.id,
                settings=settings,
            )
        s.rollback()

    with pg_session_factory() as s:
        assert s.scalar(select(TurnJob).where(TurnJob.workspace_id == wsid)) is None
        assert (
            s.scalar(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.workspace_id == wsid,
                    WorkspaceEvent.kind == "cloud_turn_sent",
                )
            )
            is None
        )
