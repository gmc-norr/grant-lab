import asyncio

import pytest

from call_manager.config import Settings
from call_manager.db.models import EvaluatorJob
from call_manager.proposal_assist.drafting.contract import TurnDoneEvent
from call_manager.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from call_manager.proposal_assist.evaluator.pass_runner import run_evaluator_pass
from tests.proposal_assist.conftest import make_workspace


def _seed_repo(workspaces_dir, ws_id):
    repo = workspaces_dir / str(ws_id) / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "proposal.md").write_text("## Aims\n\nDraft.\n")
    (repo / "proposal-spec.md").write_text("## Aims\n\nSpec.\n")


def _make_running_job(s, ws_id):
    job = EvaluatorJob(workspace_id=ws_id, status="running")
    s.add(job)
    s.flush()
    return job


@pytest.mark.asyncio
async def test_success_requires_scorecard_then_completes(
    tmp_path, pg_session_factory, _settings_env
):
    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        job = _make_running_job(s, ws.id)
        s.commit()
        ws_id, job_id = ws.id, job.id
    _seed_repo(tmp_path, ws_id)

    # Fake adapter whose send_turn writes the scorecard onto the job (simulating the
    # submit_scorecard tool), then emits TurnDoneEvent.
    class _Adapter(FakeAgentAdapter):
        async def send_turn(self, handle, user_message):
            with pg_session_factory() as s:
                j = s.get(EvaluatorJob, job_id)
                j.scorecard = {"criteria": [], "overall_band": "gap", "summary": "x"}
                s.commit()
            yield TurnDoneEvent(tokens_in=50, tokens_out=20, model="claude-x", session_id="sess")

    await run_evaluator_pass(
        job_id=job_id,
        claimed_generation=0,
        session_factory=pg_session_factory,
        settings=settings,
        adapter_builder=lambda **kw: _Adapter(),
    )
    with pg_session_factory() as s:
        done = s.get(EvaluatorJob, job_id)
        assert done.status == "completed"
        assert done.input_tokens == 50 and done.output_tokens == 20
        assert done.evaluated_draft_sha and done.evaluated_spec_sha  # staleness markers captured


@pytest.mark.asyncio
async def test_no_scorecard_fails(tmp_path, pg_session_factory, _settings_env):
    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        job = _make_running_job(s, ws.id)
        s.commit()
        ws_id, job_id = ws.id, job.id
    _seed_repo(tmp_path, ws_id)

    fake = FakeAgentAdapter()
    fake.script_next_turn([TurnDoneEvent(tokens_in=1, tokens_out=1, model="m", session_id="s")])
    await run_evaluator_pass(
        job_id=job_id,
        claimed_generation=0,
        session_factory=pg_session_factory,
        settings=settings,
        adapter_builder=lambda **kw: fake,
    )
    with pg_session_factory() as s:
        job = s.get(EvaluatorJob, job_id)
        assert job.status == "failed" and "no scorecard" in (job.error or "")


@pytest.mark.asyncio
async def test_timeout_on_launch_fails(tmp_path, pg_session_factory, _settings_env):
    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    settings.evaluator_pass_timeout_seconds = 0.1
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        job = _make_running_job(s, ws.id)
        s.commit()
        ws_id, job_id = ws.id, job.id
    _seed_repo(tmp_path, ws_id)

    class _HangLaunch(FakeAgentAdapter):
        async def launch(self, context):
            await asyncio.sleep(2)
            return await super().launch(context)

    await run_evaluator_pass(
        job_id=job_id,
        claimed_generation=0,
        session_factory=pg_session_factory,
        settings=settings,
        adapter_builder=lambda **kw: _HangLaunch(),
    )
    with pg_session_factory() as s:
        assert s.get(EvaluatorJob, job_id).status == "failed"


@pytest.mark.asyncio
async def test_cancelled_before_launch_skips_the_llm(tmp_path, pg_session_factory, _settings_env):
    # Med (Codex): a soft-delete/cancel landing AFTER the read session but BEFORE launch
    # must short-circuit the pre-launch live-status recheck — no LLM call, job stays failed.
    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        job = _make_running_job(s, ws.id)
        s.commit()
        ws_id, job_id = ws.id, job.id
    _seed_repo(tmp_path, ws_id)

    fake = FakeAgentAdapter()

    def builder(**kw):
        # Simulate the cancel committing between step-1 and _run's recheck.
        with pg_session_factory() as s:
            j = s.get(EvaluatorJob, job_id)
            j.status = "failed"
            j.error = "workspace deleted"
            s.commit()
        return fake

    await run_evaluator_pass(
        job_id=job_id,
        claimed_generation=0,
        session_factory=pg_session_factory,
        settings=settings,
        adapter_builder=builder,
    )
    assert fake.launches == []  # pre-launch recheck short-circuited — no LLM spend
    with pg_session_factory() as s:
        job = s.get(EvaluatorJob, job_id)
        assert job.status == "failed" and job.scorecard is None
