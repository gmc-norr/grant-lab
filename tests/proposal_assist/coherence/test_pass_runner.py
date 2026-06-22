import uuid

import pytest

from call_manager.config import Settings
from call_manager.db.models import CoherenceJob, ProposedChange
from call_manager.proposal_assist.coherence.pass_runner import run_coherence_pass
from call_manager.proposal_assist.drafting.contract import (
    AssistantTextEvent,
    TurnDoneEvent,
)
from call_manager.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
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
        session_factory=pg_session_factory,
        settings=settings,
        adapter_builder=lambda **kw: fake,
    )
    with pg_session_factory() as s:
        failed = s.get(CoherenceJob, job_id)
        assert failed.status == "failed" and failed.error
    # launch never returned a handle, so there is nothing to terminate
    assert len(fake.terminations) == 0
