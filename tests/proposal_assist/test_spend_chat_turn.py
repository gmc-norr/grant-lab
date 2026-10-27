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

from grant_lab.config import Settings
from grant_lab.db.models import ChatTurn, LlmSpend, TurnJob
from grant_lab.proposal_assist.drafting.claude_adapter import ClaudeAgentSDKAdapter
from grant_lab.proposal_assist.drafting.contract import (
    AgentHandle,
    AssistantTextEvent,
    TurnDoneEvent,
)
from grant_lab.proposal_assist.drafting.controller import WorkspaceController
from grant_lab.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from grant_lab.proposal_assist.drafting.registry import SubprocessRegistry
from grant_lab.proposal_assist.spend import record_spend, sum_spend_today
from grant_lab.proposal_assist.turns.outcomes import record_terminal_outcome
from grant_lab.proposal_assist.turns.processor import process_turn_once
from grant_lab.proposal_assist.turns.reaper import reap_stuck_turns
from grant_lab.proposal_assist.turns.submit import submit_user_turn
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

    By default the adapter streams an assistant text row + a ``TurnDoneEvent``
    with the given token totals; ``_finalize_success`` wins the running→completed
    UPDATE. There is deliberately NO failure script here: the one this fixture
    used to carry yielded a ``TurnDoneEvent`` and THEN raised ``SDKTurnError``,
    which the real adapter cannot do (review 2026-10-26, M-PA-1), so the ledger
    row it pinned carried a token count no real failure ever has. The failure
    paths are pinned against the REAL adapter further down this file.
    ``reap_before_finalize=True`` — after the ``TurnDoneEvent`` is yielded but
                          BEFORE ``_finalize_success`` runs, the ``turn_jobs`` row
                          is flipped to a terminal status by a BARE ``UPDATE``
                          (no ``record_terminal_outcome`` — that would itself
                          write a ledger row and defeat the exactly-once check).
                          ``_finalize_success`` then matches zero rows, rolls back,
                          and the caller reconciles through
                          ``stamp_post_terminal_usage``.
    """

    def _make(*, tokens_in, tokens_out, reap_before_finalize=False):
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

        if reap_before_finalize:

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


def test_a_completed_turn_records_its_spend(
    pg_session_factory, make_finished_turn, _settings_env
):
    # `make_finished_turn` drives the real processor path to a terminal state.
    ws_id, job_id, assistant_id = make_finished_turn(tokens_in=11, tokens_out=13)
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
        assert sum_spend_today(s, settings=Settings(), workspace_id=ws_id) == (11, 13)


def test_one_turn_writes_exactly_one_row(pg_session_factory, make_finished_turn):
    # A retry loop or a double-finalize would double-count against the cap.
    make_finished_turn(tokens_in=1, tokens_out=1)
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
        tokens_in=5, tokens_out=6, reap_before_finalize=True
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


def test_a_CLAIMED_chat_turn_refuses_when_the_global_cap_is_reached(
    pg_session_factory, _settings_env, monkeypatch
):
    """submit_user_turn checked the global cap at ENQUEUE time. That answer goes
    stale: a launch failure leaves the row `queued` and the next drain tick
    retries it, so the accepted-at decision can be hours old by dispatch.

    The four pass runners recheck before launching; chat was the one hosted
    family that did not — the "contributes to the total, never consults it"
    shape this branch exists to remove, surviving inside the branch that removes
    it. The cap is seeded over the ceiling AFTER submit, so only the processor's
    own recheck can stop it, and the adapter raises if it is ever reached.
    """
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
            content_text="Hi",
            submitted_by_user_id=uid,
            settings=settings,
        )
        s.commit()
        job_id = job.id

    # Over the ceiling only NOW — after the enqueue-time check has passed.
    monkeypatch.setenv("GLOBAL_DAILY_TOKEN_CAP_INPUT", "100")
    with pg_session_factory() as s:
        record_spend(
            s,
            workspace_id=wsid,
            kind="chat_turn",
            status="ok",
            tokens_in=5000,
            tokens_out=0,
        )
        s.commit()

    adapter = FakeAgentAdapter()

    async def _must_not_run(handle, msg):
        # NB: this message must not contain the word the assertion below greps
        # for. A first draft said "despite the global cap" — the processor
        # wraps a raised exception into turn_jobs.error, so the mutant's
        # `processor_failure: ...global...` satisfied a substring assert and
        # the mutation stayed green.
        raise AssertionError("send_turn reached; the ceiling was not consulted")
        yield  # pragma: no cover — makes this an async generator

    adapter.send_turn = _must_not_run  # type: ignore[method-assign]
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry, session_factory=pg_session_factory
    )

    asyncio.run(
        process_turn_once(
            session_factory=pg_session_factory,
            controller=controller,
            turn_job_id=job_id,
        )
    )

    with pg_session_factory() as s:
        turn_job = s.get(TurnJob, job_id)
        assert turn_job.status == "failed"
        assert turn_job.error == "global daily token cap reached"
        rows = (
            s.execute(
                select(LlmSpend).where(
                    LlmSpend.kind == "chat_turn", LlmSpend.ref_id == str(job_id)
                )
            )
            .scalars()
            .all()
        )
        assert [(r.tokens_in, r.tokens_out) for r in rows] == [(0, 0)], rows


# ---------------------------------------------------------------------------
# M-PA-1 — a turn that fails, times out or is cancelled records what it billed
#
# These drive the REAL `ClaudeAgentSDKAdapter.send_turn` with a fake SDK client,
# through the REAL `process_turn_once` — the shape of review 2026-10-26's
# PROBE-A/PROBE-B. The test these replace (`test_a_FAILED_turn_still_records`)
# stubbed `adapter.send_turn` with a generator that yielded a `TurnDoneEvent`
# and THEN raised: an event order the real adapter cannot produce, because
# `send_turn` raises on an error `ResultMessage` BEFORE it reads the usage that
# would have filled that event. So the ledger row it pinned carried a token
# count no real failure ever had, and the measured truth was
#
#     ledger=[('chat_turn','failed',0,0)] sum_spend_today=(0,0) ws_cap_used=(0,0)
#
# for both the error-result and the timeout case — the two most expensive turns
# there are, invisible to the per-workspace cap AND the global one.
# ---------------------------------------------------------------------------

# A5's probe numbers: an error result that had already billed ~400k in / 30k out.
_ERROR_RESULT_USAGE = {
    "input_tokens": 12_000,
    "cache_creation_input_tokens": 380_000,
    "cache_read_input_tokens": 8_000,
    "output_tokens": 30_000,
}
_ERROR_RESULT_BILLED = (400_000, 30_000)

# The running AssistantMessage tally a cancelled turn has already spent: the
# LAST message's counts, which is what the adapter keeps.
_STREAMED_TALLY = (3_000, 300)

# What `submit_user_turn` pre-stamped on the user row (the attachment/reference
# estimate). Passed explicitly rather than owned through an Attachment row —
# the same seam `submit_user_turn(tokens_in=...)` exists for.
_PRE_COUNT = 5_000


def _sdk_assistant_messages():
    """Three billed `AssistantMessage`s, exactly as the probe streamed them."""
    from claude_agent_sdk.types import AssistantMessage, TextBlock

    return [
        AssistantMessage(
            content=[TextBlock(text=f"chunk {n}")],
            model="claude-opus-5",
            usage={"input_tokens": 1_000 * n, "output_tokens": 100 * n},
        )
        for n in (1, 2, 3)
    ]


def _sdk_error_result():
    from claude_agent_sdk.types import ResultMessage

    rm = object.__new__(ResultMessage)
    rm.is_error = True
    rm.api_error_status = None
    rm.errors = ["error_max_turns"]
    rm.usage = dict(_ERROR_RESULT_USAGE)
    rm.model_usage = None
    rm.session_id = "sess-failed"
    rm.uuid = "sess-failed"
    return rm


class _FakeClientSDKAdapter(ClaudeAgentSDKAdapter):
    """The REAL adapter with only `launch` replaced.

    `launch` would start a `claude` CLI subprocess, so it hands back a handle
    carrying a fake client instead. Everything the turn is actually about —
    `send_turn`'s message loop, its usage extraction and its raises — is
    production's own code. Stubbing `send_turn` is what made the predecessor
    test hollow.
    """

    def __init__(self, *, workspaces_dir: str, receive) -> None:
        super().__init__(
            model="claude-opus-5",
            workspaces_dir=workspaces_dir,
            # send_turn fails closed without an org key (F6). Never a real one.
            anthropic_api_key="sk-ant-api03-FAKE-not-a-real-key",
        )
        self._receive = receive

    async def launch(self, context):
        from unittest.mock import AsyncMock, MagicMock

        client = MagicMock()
        client.query = AsyncMock()
        client.disconnect = AsyncMock()
        client.receive_response = self._receive
        return AgentHandle(workspace_id=context.workspace_id, state={"client": client})


@pytest.fixture
def run_a_real_adapter_turn(pg_session_factory, _settings_env, tmp_path):
    """Run one turn through `process_turn_once` with the real adapter.

    `cancel_on` is how the wall-clock bound is reproduced WITHOUT a race. What
    `drain.py` does at 600 s is `asyncio.wait_for(coro, turn_max_seconds)`,
    whose only effect is to cancel the turn's coroutine — "which raises
    CancelledError at the streaming await inside process_turn_once", in its own
    words. A short `wait_for` here would be a coin flip between that await and
    the per-event DB writes next to it, so the fake stream SIGNALS when it has
    reached the hang and the cancel is delivered then: the same cancel, at the
    same await, every run and under any load.

    Returns ``(workspace_id, turn_job_id, user_turn_id, outcome)``.
    """

    def _run(*, receive, cancel_on=None, pre_count=_PRE_COUNT):
        settings = Settings()
        adapter = _FakeClientSDKAdapter(workspaces_dir=str(tmp_path), receive=receive)

        with pg_session_factory() as s:
            user = make_user(s)
            ws = make_workspace(s, owner=user)
            s.commit()
            uid, wsid = user.id, ws.id
        with pg_session_factory() as s:
            job = submit_user_turn(
                session=s,
                workspace_id=wsid,
                content_text="Draft the impact section",
                submitted_by_user_id=uid,
                settings=settings,
                tokens_in=pre_count,
            )
            s.commit()
            job_id, user_turn_id = job.id, job.user_turn_id

        registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
        controller = WorkspaceController(
            registry=registry,
            session_factory=pg_session_factory,
        )

        async def _go():
            task = asyncio.create_task(
                process_turn_once(
                    session_factory=pg_session_factory,
                    controller=controller,
                    turn_job_id=job_id,
                    workspaces_dir=tmp_path,
                    settings=settings,
                )
            )
            if cancel_on is None:
                await task
                return "returned"
            # Safety net only — the stream sets this within milliseconds of
            # reaching its hang. A timeout here means the fake never got there.
            await asyncio.wait_for(cancel_on.wait(), timeout=10)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                # process_turn_once finalizes and then re-raises a
                # non-Exception BaseException, which is what drain.py's
                # wait_for catches and reports as turn_timed_out.
                return "cancelled"
            return "returned"

        outcome = asyncio.run(_go())
        return wsid, job_id, user_turn_id, outcome

    return _run


def _ws_cap_used(session, workspace_id):
    """What the PER-WORKSPACE cap sees. It sums the chat_turns columns, not the
    ledger (`turns/caps.py::check_daily_token_cap`), so it is the reader the
    nulled pre-count actually starved."""
    from grant_lab.proposal_assist.turns.caps import check_daily_token_cap

    r = check_daily_token_cap(session, workspace_id, settings=Settings())
    return r.used_in, r.used_out


def test_a_turn_that_fails_on_an_error_result_records_what_that_result_billed(
    pg_session_factory, run_a_real_adapter_turn
):
    """PROBE-B[error_result]. `send_turn` raised on the error `ResultMessage`
    BEFORE reading its usage, so ~400k input tokens the org had already been
    billed for reached neither cap. An `error_max_turns` result is the shape of
    a looping workspace — the one the caps exist to stop."""

    async def receive():
        for message in _sdk_assistant_messages():
            yield message
        yield _sdk_error_result()

    ws_id, job_id, user_turn_id, _ = run_a_real_adapter_turn(receive=receive)

    with pg_session_factory() as s:
        job = s.get(TurnJob, job_id)
        assert job.status == "failed"
        rows = _chat_rows(s)
        assert len(rows) == 1, rows
        assert rows[0].status == "failed"
        assert rows[0].ref_id == str(job_id)
        assert (rows[0].tokens_in, rows[0].tokens_out) == _ERROR_RESULT_BILLED
        assert (
            sum_spend_today(s, settings=Settings(), workspace_id=ws_id)
            == _ERROR_RESULT_BILLED
        )
        # And the per-workspace cap, which reads the columns: the submit-time
        # estimate is SUPERSEDED by the billed truth on the outcome row, never
        # dropped. (400k is what was really spent; 5k was the guess.)
        assert _ws_cap_used(s, ws_id) == _ERROR_RESULT_BILLED
        outcome_row = s.scalar(
            select(ChatTurn).where(ChatTurn.workspace_id == ws_id, ChatTurn.role == "system")
        )
        assert (outcome_row.tokens_in, outcome_row.tokens_out) == _ERROR_RESULT_BILLED


def test_a_timed_out_turn_records_the_tally_it_had_already_streamed(
    pg_session_factory, run_a_real_adapter_turn
):
    """PROBE-B[timeout]. The 600 s `TURN_MAX_SECONDS` cancel discarded the
    running `AssistantMessage` tally entirely. The tally is what to record: the
    ResultMessage never arrived, so its total is not available and must not be
    invented."""

    hung = asyncio.Event()

    async def receive():
        for message in _sdk_assistant_messages():
            yield message
        hung.set()
        await asyncio.sleep(3600)  # the hang the wall-clock bound exists for

    ws_id, job_id, user_turn_id, outcome = run_a_real_adapter_turn(
        receive=receive, cancel_on=hung
    )
    assert outcome == "cancelled"

    with pg_session_factory() as s:
        assert s.get(TurnJob, job_id).status == "failed"
        rows = _chat_rows(s)
        assert len(rows) == 1, rows
        assert (rows[0].tokens_in, rows[0].tokens_out) == _STREAMED_TALLY
        assert (
            sum_spend_today(s, settings=Settings(), workspace_id=ws_id) == _STREAMED_TALLY
        )
        assert _ws_cap_used(s, ws_id) == _STREAMED_TALLY


def test_a_turn_cancelled_before_it_billed_anything_keeps_its_pre_count(
    pg_session_factory, run_a_real_adapter_turn
):
    """The other half of the finding: `record_terminal_outcome` NULLED the user
    turn's submit-time estimate on the way out. A turn cancelled mid-flight was
    then cheaper to the cap than one never submitted — so a looping workspace
    could keep the cap at zero by timing out.

    Nothing measurable was billed here (no `AssistantMessage` arrived), but the
    request DID go out, so the estimate is retained rather than erased —
    `record_terminal_outcome`'s own `usage_unknown` contract."""

    hung = asyncio.Event()

    async def receive():
        hung.set()
        await asyncio.sleep(3600)
        yield _sdk_error_result()  # pragma: no cover — never reached

    ws_id, job_id, user_turn_id, outcome = run_a_real_adapter_turn(
        receive=receive, cancel_on=hung
    )
    assert outcome == "cancelled"

    with pg_session_factory() as s:
        assert s.get(TurnJob, job_id).status == "failed"
        user_turn = s.get(ChatTurn, user_turn_id)
        assert user_turn.tokens_in == _PRE_COUNT
        assert _ws_cap_used(s, ws_id) == (_PRE_COUNT, 0)
        rows = _chat_rows(s)
        assert len(rows) == 1, rows
        assert rows[0].status == "failed"
