"""A finished chat turn leaves exactly one ledger row — including when it failed.

The old code stamped chat_turns.tokens_* on success and the cap summed that
table. A turn that errored after consuming input tokens was invisible.

`ref_id` is the TurnJob id: one TurnJob is one hosted call, and it is the only
identity in scope at all three recording points (_finalize_success,
record_terminal_outcome, stamp_post_terminal_usage).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from call_manager.config import Settings
from call_manager.db.models import ChatTurn, LlmSpend, TurnJob
from call_manager.proposal_assist.drafting.contract import (
    AssistantTextEvent,
    SDKTurnError,
    TurnDoneEvent,
)
from call_manager.proposal_assist.drafting.controller import WorkspaceController
from call_manager.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from call_manager.proposal_assist.drafting.registry import SubprocessRegistry
from call_manager.proposal_assist.spend import sum_spend_today
from call_manager.proposal_assist.turns.outcomes import record_terminal_outcome
from call_manager.proposal_assist.turns.processor import process_turn_once
from call_manager.proposal_assist.turns.reaper import reap_stuck_turns
from call_manager.proposal_assist.turns.submit import submit_user_turn
from tests.proposal_assist.conftest import make_user, make_workspace


def _chat_rows(session):
    return (
        session.execute(select(LlmSpend).where(LlmSpend.kind == "chat_turn"))
        .scalars()
        .all()
    )


@pytest.fixture
def make_finished_turn(pg_session_factory, _settings_env):
    """Drive the REAL processor path to a terminal state and return
    ``(workspace_id, turn_job_id, assistant_chat_turn_id)``.

    This is deliberately NOT a hand-inserted ``LlmSpend`` row — it runs
    ``process_turn_once`` with a stubbed adapter exactly the way
    ``test_turns_processor.py`` does, so the ledger row it asserts on is written
    by production's own finalize path. A hand-written row would prove the ORM,
    not the wiring.

    ``status="ok"``     — the adapter streams an assistant text row + a
                          ``TurnDoneEvent`` with the given token totals;
                          ``_finalize_success`` wins the running→completed UPDATE.
    ``status="failed"`` — the adapter yields the ``TurnDoneEvent`` (so the
                          consumed tokens are known) and then raises
                          ``SDKTurnError``; ``_finalize_failed`` routes through
                          ``record_terminal_outcome``. There is no assistant row
                          on this path (a viewer-safe ``system`` row is written
                          instead), so ``assistant_chat_turn_id`` is ``None``.
    ``reap_before_finalize=True`` — after the ``TurnDoneEvent`` is yielded but
                          BEFORE ``_finalize_success`` runs, the ``turn_jobs`` row
                          is flipped to a terminal status by a BARE ``UPDATE``
                          (no ``record_terminal_outcome`` — that would itself
                          write a ledger row and defeat the exactly-once check).
                          ``_finalize_success`` then matches zero rows, rolls back,
                          and the caller reconciles through
                          ``stamp_post_terminal_usage``.
    """

    def _make(*, tokens_in, tokens_out, status, reap_before_finalize=False):
        settings = Settings()
        adapter = FakeAgentAdapter()

        with pg_session_factory() as s:
            user = make_user(s)
            ws = make_workspace(s, owner=user)
            s.commit()
            uid, wsid = user.id, ws.id
        with pg_session_factory() as s:
            job = submit_user_turn(
                session=s,
                workspace_id=wsid,
                content_text="Hi",
                submitted_by_user_id=uid,
                settings=settings,
            )
            s.commit()
            job_id = job.id

        done = TurnDoneEvent(
            tokens_in=tokens_in, tokens_out=tokens_out, model="fake", session_id="s1"
        )

        if status == "failed":

            async def _fail(handle, msg):
                # Report the consumed tokens FIRST, so the processor captures
                # them, THEN fail — the whole point of the ledger is that a turn
                # that spent input before erroring still records that spend.
                yield done
                raise SDKTurnError("boom", status="500")

            adapter.send_turn = _fail  # type: ignore[method-assign]
        elif reap_before_finalize:

            async def _reap_mid_stream(handle, msg):
                yield AssistantTextEvent(text="ok")
                yield done
                # The reaper (or a delete) gets the row FIRST: flip it terminal
                # with a bare UPDATE, so _finalize_success's conditional
                # running→completed UPDATE matches zero rows. record_terminal_outcome
                # is deliberately NOT used here — it would write its own ledger row
                # and turn this into a two-row case.
                with pg_session_factory() as s2:
                    s2.execute(
                        update(TurnJob)
                        .where(TurnJob.id == job_id, TurnJob.status == "running")
                        .values(status="failed", completed_at=datetime.now(UTC))
                    )
                    s2.commit()

            adapter.send_turn = _reap_mid_stream  # type: ignore[method-assign]
        else:
            adapter.script_next_turn([AssistantTextEvent(text="ok"), done])

        registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
        controller = WorkspaceController(
            registry=registry,
            session_factory=pg_session_factory,
        )

        asyncio.run(
            process_turn_once(
                session_factory=pg_session_factory,
                controller=controller,
                turn_job_id=job_id,
            )
        )

        with pg_session_factory() as s:
            assistant = s.scalar(
                select(ChatTurn).where(
                    ChatTurn.workspace_id == wsid, ChatTurn.role == "assistant"
                )
            )
            assistant_id = assistant.id if assistant is not None else None
        return wsid, job_id, assistant_id

    return _make


@pytest.fixture
def make_running_turn_job(pg_session_factory):
    """Insert a ``running`` ``turn_jobs`` row directly and return
    ``(workspace_id, turn_job_id)``.

    This does NOT drive the processor: the reaper and ``record_terminal_outcome``
    tests exercise ``reaper.py`` / ``outcomes.py`` directly, which is the point —
    those are the terminal paths no processor test reaches.
    """

    def _make(*, started_seconds_ago):
        with pg_session_factory() as s:
            user = make_user(s)
            ws = make_workspace(s, owner=user)
            s.flush()
            job = TurnJob(
                workspace_id=ws.id,
                status="running",
                submitted_by_user_id=user.id,
                started_at=datetime.now(UTC) - timedelta(seconds=started_seconds_ago),
            )
            s.add(job)
            s.commit()
            return ws.id, job.id

    return _make


def test_a_completed_turn_records_its_spend(pg_session_factory, make_finished_turn):
    # `make_finished_turn` drives the real processor path to a terminal state.
    ws_id, job_id, assistant_id = make_finished_turn(
        tokens_in=11, tokens_out=13, status="ok"
    )
    with pg_session_factory() as s:
        row = s.execute(
            select(LlmSpend).where(LlmSpend.kind == "chat_turn")
        ).scalar_one()
        assert row.ref_id == str(job_id)
        assert (row.tokens_in, row.tokens_out) == (11, 13)
        assert row.status == "ok"
        # The display cache still agrees with the ledger. On the success path the
        # SDK totals land on the ASSISTANT row and the user row's submit-time
        # pre-count is cleared (processor.py:583-585, :593-594).
        turn = s.get(ChatTurn, assistant_id)
        assert (turn.tokens_in, turn.tokens_out) == (11, 13)
        assert sum_spend_today(s, workspace_id=ws_id) == (11, 13)


def test_a_FAILED_turn_still_records(pg_session_factory, make_finished_turn):
    # THE regression. Spend happened; the old code recorded nothing in a ledger,
    # and Task 7 is about to stop the cap reading the column that carried it.
    ws_id, job_id, _ = make_finished_turn(tokens_in=7, tokens_out=0, status="failed")
    with pg_session_factory() as s:
        row = s.execute(
            select(LlmSpend).where(LlmSpend.kind == "chat_turn")
        ).scalar_one()
        assert row.ref_id == str(job_id)
        assert row.status == "failed"
        assert row.tokens_in == 7


def test_one_turn_writes_exactly_one_row(pg_session_factory, make_finished_turn):
    # A retry loop or a double-finalize would double-count against the cap.
    make_finished_turn(tokens_in=1, tokens_out=1, status="ok")
    with pg_session_factory() as s:
        assert len(_chat_rows(s)) == 1


def test_a_turn_reaped_before_finalize_still_records_exactly_one_row(
    pg_session_factory, make_finished_turn
):
    # _finalize_success's conditional running→completed UPDATE matches zero rows
    # when the reaper got there first; it then calls s.rollback() and returns
    # False (processor.py:621-630), which DISCARDS any record_spend written above
    # it. The caller reconciles through _reconcile_lost_usage →
    # stamp_post_terminal_usage (processor.py:496, :512, :532), and that is the
    # branch that must carry the row. One row — not zero, and not two.
    ws_id, job_id, _ = make_finished_turn(
        tokens_in=5, tokens_out=6, status="ok", reap_before_finalize=True
    )
    with pg_session_factory() as s:
        rows = _chat_rows(s)
        assert len(rows) == 1
        assert rows[0].ref_id == str(job_id)
        assert (rows[0].tokens_in, rows[0].tokens_out) == (5, 6)


def test_a_reaped_turn_records_its_consumed_tokens(
    pg_session_factory, make_running_turn_job
):
    # The REAPER path, not the processor path: reaper.py:73 is one of the five
    # record_terminal_outcome call sites outside processor.py. Before this task
    # its spend reached the cap only via ChatTurn.tokens_*, which Task 7 stops
    # reading.
    ws_id, job_id = make_running_turn_job(started_seconds_ago=3600)
    reap_stuck_turns(session_factory=pg_session_factory, ttl_seconds=60)
    with pg_session_factory() as s:
        rows = _chat_rows(s)
        assert len(rows) == 1
        assert rows[0].ref_id == str(job_id)
        assert rows[0].status == "failed"


def test_an_already_terminal_turn_records_nothing_twice(
    pg_session_factory, make_running_turn_job
):
    # record_terminal_outcome's conditional UPDATE returns no row when the job is
    # already terminal, and the function returns False having written nothing
    # (outcomes.py:78-79). record_spend must sit BELOW that guard: it is the only
    # thing standing between a double-finalize and a double-charge, because
    # llm_spend has no unique call-identity constraint.
    ws_id, job_id = make_running_turn_job(started_seconds_ago=0)
    for _ in range(2):
        with pg_session_factory() as s:
            record_terminal_outcome(
                s,
                turn_job_id=job_id,
                workspace_id=ws_id,
                outcome="failed",
                expected_status="running",
                raw_error="boom",
                emit_chat=True,
                tokens_in=4,
                tokens_out=2,
            )
            s.commit()
    with pg_session_factory() as s:
        rows = _chat_rows(s)
        assert len(rows) == 1
        assert (rows[0].tokens_in, rows[0].tokens_out) == (4, 2)
