import uuid

import pytest

from grant_lab.config import Settings
from grant_lab.db.models import CoherenceJob, ProposedChange
from grant_lab.proposal_assist.coherence.pass_runner import run_coherence_pass
from grant_lab.proposal_assist.drafting.contract import (
    AssistantTextEvent,
    TurnDoneEvent,
)
from grant_lab.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from tests.proposal_assist.conftest import make_workspace


def _seed_repo(workspaces_dir, ws_id):
    repo = workspaces_dir / str(ws_id) / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "proposal.md").write_text("## Aims\n\nDraft.\n")
    (repo / "proposal-spec.md").write_text("## Aims\n\nSpec.\n")


def _make_job(s, ws_id):
    job = CoherenceJob(workspace_id=ws_id, status="running")
    s.add(job)
    s.flush()
    return job


@pytest.mark.asyncio
async def test_success_records_tokens_supersedes_prior_and_completes(
    tmp_path, pg_session_factory, _settings_env
):
    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        job = _make_job(s, ws.id)
        # a prior pass's pending row that THIS pass should supersede
        prior_job = uuid.uuid4()
        s.add(
            ProposedChange(
                workspace_id=ws.id,
                target_artifact="draft",
                section_index=0,
                base_commit_sha="x",
                base_section_sha="y",
                kind="prose_edit",
                payload={"new_text": "old"},
                origin_kind="coherence_session",
                origin_job_id=prior_job,
                status="pending",
            )
        )
        s.commit()
        ws_id, job_id = ws.id, job.id
    _seed_repo(tmp_path, ws_id)

    fake = FakeAgentAdapter()
    fake.script_next_turn(
        [
            AssistantTextEvent(text="checking"),
            TurnDoneEvent(tokens_in=120, tokens_out=30, model="claude-x", session_id="sess-1"),
        ]
    )

    await run_coherence_pass(
        job_id=job_id,
        claimed_generation=0,
        session_factory=pg_session_factory,
        settings=settings,
        adapter_builder=lambda **kw: fake,
    )

    with pg_session_factory() as s:
        done = s.get(CoherenceJob, job_id)
        assert done.status == "completed"
        assert done.input_tokens == 120 and done.output_tokens == 30
        assert done.model == "claude-x" and done.sdk_session_id == "sess-1"
        prior = s.query(ProposedChange).filter_by(origin_job_id=prior_job).one()
        assert prior.status == "superseded"
    assert len(fake.terminations) == 1  # adapter closed


@pytest.mark.asyncio
async def test_failure_marks_failed_and_supersedes_own_partial(
    tmp_path, pg_session_factory, _settings_env
):
    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        job = _make_job(s, ws.id)
        s.commit()
        ws_id, job_id = ws.id, job.id
    _seed_repo(tmp_path, ws_id)

    # a partial row already emitted by THIS job before the crash
    with pg_session_factory() as s:
        s.add(
            ProposedChange(
                workspace_id=ws_id,
                target_artifact="draft",
                section_index=0,
                base_commit_sha="x",
                base_section_sha="y",
                kind="prose_edit",
                payload={"new_text": "partial"},
                origin_kind="coherence_session",
                origin_job_id=job_id,
                status="pending",
            )
        )
        s.commit()

    class _Boom(FakeAgentAdapter):
        async def send_turn(self, handle, user_message):
            raise RuntimeError("sdk exploded")
            yield  # pragma: no cover  (make it an async generator)

    fake = _Boom()
    await run_coherence_pass(
        job_id=job_id,
        claimed_generation=0,
        session_factory=pg_session_factory,
        settings=settings,
        adapter_builder=lambda **kw: fake,
    )

    with pg_session_factory() as s:
        failed = s.get(CoherenceJob, job_id)
        assert failed.status == "failed" and failed.error
        partial = s.query(ProposedChange).filter_by(origin_job_id=job_id).one()
        assert partial.status == "superseded"
    assert len(fake.terminations) == 1  # terminated in finally even on error


@pytest.mark.asyncio
async def test_in_pass_timeout_fails_and_terminates(tmp_path, pg_session_factory, _settings_env):
    import asyncio

    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    settings.coherence_pass_timeout_seconds = 0.1  # force the wait_for to trip
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        job = _make_job(s, ws.id)
        s.commit()
        ws_id, job_id = ws.id, job.id
    _seed_repo(tmp_path, ws_id)

    class _Hang(FakeAgentAdapter):
        async def send_turn(self, handle, user_message):
            await asyncio.sleep(5)  # longer than the 0.1s pass timeout
            yield  # pragma: no cover

    fake = _Hang()
    await run_coherence_pass(
        job_id=job_id,
        claimed_generation=0,
        session_factory=pg_session_factory,
        settings=settings,
        adapter_builder=lambda **kw: fake,
    )
    with pg_session_factory() as s:
        failed = s.get(CoherenceJob, job_id)
        assert failed.status == "failed" and failed.error
    assert len(fake.terminations) == 1  # adapter terminated in finally on timeout


@pytest.mark.asyncio
async def test_in_pass_timeout_on_launch_fails(tmp_path, pg_session_factory, _settings_env):
    # A hung SDK *connect* (launch) must also be bounded by the pass timeout — not just
    # the streaming turn. Before the fix only _drive() was wrapped in wait_for, so a
    # hung launch returned eventually and the job COMPLETED instead of failing.
    import asyncio

    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    settings.coherence_pass_timeout_seconds = 0.1
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        job = _make_job(s, ws.id)
        s.commit()
        ws_id, job_id = ws.id, job.id
    _seed_repo(tmp_path, ws_id)

    class _HangLaunch(FakeAgentAdapter):
        async def launch(self, context):
            await asyncio.sleep(2)  # longer than the 0.1s pass timeout
            return await super().launch(context)

    fake = _HangLaunch()
    await run_coherence_pass(
        job_id=job_id,
        claimed_generation=0,
        session_factory=pg_session_factory,
        settings=settings,
        adapter_builder=lambda **kw: fake,
    )
    with pg_session_factory() as s:
        failed = s.get(CoherenceJob, job_id)
        assert failed.status == "failed" and failed.error
    # launch never returned a handle, so there is nothing to terminate
    assert len(fake.terminations) == 0


# ---------------------------------------------------------------------------
# M-PA-1 — a pass that fails before its ResultMessage still records its spend
#
# One of the four background pass families, driven with the REAL adapter's
# `send_turn` and a fake SDK client. All four reach the billed tally through the
# SAME shared helper (`contract.done_or_billed`, one line at the top of each
# `except` block), and `tests/proposal_assist/test_failed_turns_bill_and_bound.py`
# gates that none of them loses it.
# ---------------------------------------------------------------------------


class _RealSendTurnKeeper:
    """The real `ClaudeAgentSDKAdapter` with only `launch` replaced.

    `launch` would start a `claude` CLI subprocess. Everything the finding is
    about — `send_turn`'s usage extraction and its raise — is production code.
    """

    def __new__(cls, *, workspaces_dir, receive):
        from unittest.mock import AsyncMock, MagicMock

        from grant_lab.proposal_assist.drafting.claude_adapter import (
            ClaudeAgentSDKAdapter,
        )
        from grant_lab.proposal_assist.drafting.contract import AgentHandle

        class _Adapter(ClaudeAgentSDKAdapter):
            async def launch(self, context):
                client = MagicMock()
                client.query = AsyncMock()
                client.disconnect = AsyncMock()
                client.receive_response = receive
                return AgentHandle(
                    workspace_id=context.workspace_id, state={"client": client}
                )

        return _Adapter(
            model="claude-opus-5",
            workspaces_dir=workspaces_dir,
            # send_turn fails closed without an org key (F6). Never a real one.
            anthropic_api_key="sk-ant-api03-FAKE-not-a-real-key",
        )


@pytest.mark.asyncio
async def test_a_pass_that_dies_on_an_error_result_records_what_it_billed(
    tmp_path, pg_session_factory, _settings_env
):
    """`done` is None on every failure path (no TurnDoneEvent is emitted), so
    the job's columns and its ledger row both recorded 0 for a call that had
    really billed ~400k input tokens — invisible to the coherence daily cap."""
    from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock

    from grant_lab.db.models import LlmSpend

    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        job = _make_job(s, ws.id)
        s.commit()
        ws_id, job_id = ws.id, job.id
    _seed_repo(tmp_path, ws_id)

    async def receive():
        yield AssistantMessage(
            content=[TextBlock(text="looping")],
            model="claude-opus-5",
            usage={"input_tokens": 3_000, "output_tokens": 300},
        )
        rm = object.__new__(ResultMessage)
        rm.is_error = True
        rm.api_error_status = None
        rm.errors = ["error_max_turns"]
        rm.usage = {
            "input_tokens": 12_000,
            "cache_creation_input_tokens": 380_000,
            "cache_read_input_tokens": 8_000,
            "output_tokens": 30_000,
        }
        rm.model_usage = None
        rm.session_id = "sess-looped"
        yield rm

    await run_coherence_pass(
        job_id=job_id,
        claimed_generation=0,
        session_factory=pg_session_factory,
        settings=settings,
        adapter_builder=lambda **kw: _RealSendTurnKeeper(
            workspaces_dir=str(tmp_path), receive=receive
        ),
    )

    with pg_session_factory() as s:
        failed = s.get(CoherenceJob, job_id)
        assert failed.status == "failed"
        # The display cache the UI reads...
        assert (failed.input_tokens, failed.output_tokens) == (400_000, 30_000)
        # ...and the ledger every cap reads. (`_fail` does not stamp
        # CoherenceJob.model on this path and that is pre-existing; the ledger
        # row carries the model, which is where the billing question is asked.)
        row = s.query(LlmSpend).filter_by(kind="coherence").one()
        assert row.status == "failed"
        assert (row.tokens_in, row.tokens_out) == (400_000, 30_000)
        assert row.model == "claude-opus-5"


@pytest.mark.asyncio
async def test_a_pass_that_times_out_mid_stream_records_what_it_had_already_billed(
    tmp_path, pg_session_factory, _settings_env
):
    """The timeout path, which the error-result test above does NOT reach.

    All four pass families stream inside `asyncio.wait_for`. On expiry,
    `asyncio.timeouts.Timeout.__aexit__` does `raise TimeoutError from exc_val`,
    so the `CancelledError` that `send_turn` attached the tally to survives only
    on `__cause__` — and the first fix pass read the attachment off the raised
    exception alone, so every timed-out pass still recorded 0. That is the
    runaway the per-workspace daily caps exist to stop: a pass that burns its
    whole timeout is exactly the expensive one.

    Driven with the REAL adapter's `send_turn` (only `launch` replaced) through
    the real `run_coherence_pass`, so the exception chain is production's own.
    """
    import asyncio

    from claude_agent_sdk.types import AssistantMessage, TextBlock

    from grant_lab.db.models import LlmSpend

    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    # Generous relative to a pooled single-row SELECT plus an instant fake
    # launch, so under `-n 16` the deadline still lands in the stream. If it
    # ever does not, `reached_hang` below fails the test loudly rather than
    # letting it pass on an empty tally.
    settings.coherence_pass_timeout_seconds = 2.0
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        job = _make_job(s, ws.id)
        s.commit()
        ws_id, job_id = ws.id, job.id
    _seed_repo(tmp_path, ws_id)

    reached_hang = asyncio.Event()
    never = asyncio.Event()  # deliberately never set: the hung SDK

    async def receive():
        yield AssistantMessage(
            content=[TextBlock(text="thinking")],
            model="claude-opus-5",
            usage={"input_tokens": 3_000, "output_tokens": 300},
        )
        # Resumed only once the adapter has consumed (and tallied) the message
        # above, so the attachment is non-empty by the time the deadline lands.
        reached_hang.set()
        await never.wait()
        yield  # pragma: no cover

    await run_coherence_pass(
        job_id=job_id,
        claimed_generation=0,
        session_factory=pg_session_factory,
        settings=settings,
        adapter_builder=lambda **kw: _RealSendTurnKeeper(
            workspaces_dir=str(tmp_path), receive=receive
        ),
    )

    assert reached_hang.is_set(), (
        "the pass timed out before the stream billed anything, so this run "
        "could not have measured the attachment — not a pass"
    )

    with pg_session_factory() as s:
        failed = s.get(CoherenceJob, job_id)
        assert failed.status == "failed"
        # There is no ResultMessage on this path, so the running
        # AssistantMessage tally is the only measured number there is.
        assert (failed.input_tokens, failed.output_tokens) == (3_000, 300)
        row = s.query(LlmSpend).filter_by(kind="coherence").one()
        assert row.status == "timeout"
        assert (row.tokens_in, row.tokens_out) == (3_000, 300)
        assert row.model == "claude-opus-5"
