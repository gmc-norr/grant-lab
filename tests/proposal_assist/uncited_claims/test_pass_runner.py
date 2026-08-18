import asyncio

import pytest

from call_manager.config import Settings
from call_manager.db.models import UncitedClaimsJob
from call_manager.proposal_assist.drafting.contract import TurnDoneEvent
from call_manager.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from call_manager.proposal_assist.uncited_claims.pass_runner import run_uncited_claims_pass
from tests.proposal_assist.conftest import make_workspace


def _seed_repo(workspaces_dir, ws_id):
    repo = workspaces_dir / str(ws_id) / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    # section 1 contains the verbatim claim text; section 0 is the (empty) preamble.
    (repo / "proposal.md").write_text("## Aims\n\nWe claim 30% relapse occurs.\n")


def _running_job(s, ws_id):
    job = UncitedClaimsJob(workspace_id=ws_id, status="running")
    s.add(job)
    s.flush()
    return job


@pytest.mark.asyncio
async def test_success_filters_hashes_and_dedupes(tmp_path, pg_session_factory, _settings_env):
    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        job = _running_job(s, ws.id)
        s.commit()
        ws_id, job_id = ws.id, job.id
    _seed_repo(tmp_path, ws_id)

    class _Adapter(FakeAgentAdapter):
        async def send_turn(self, handle, user_message):
            with pg_session_factory() as s:
                j = s.get(UncitedClaimsJob, job_id)
                # one real (substring of §1), one duplicate of it, one hallucinated (not present)
                j.claims = [
                    {"claim_text": "30% relapse", "section_index": 1, "reason": "stat"},
                    {"claim_text": "30% relapse", "section_index": 1, "reason": "stat dup"},
                    {"claim_text": "made up fact", "section_index": 1, "reason": "halluc"},
                ]
                s.commit()
            yield TurnDoneEvent(tokens_in=10, tokens_out=5, model="m", session_id="s")

    await run_uncited_claims_pass(
        job_id=job_id, claimed_generation=0, session_factory=pg_session_factory, settings=settings,
        adapter_builder=lambda **kw: _Adapter(),
    )
    with pg_session_factory() as s:
        done = s.get(UncitedClaimsJob, job_id)
        assert done.status == "completed"
        assert len(done.claims) == 1  # dup deduped, hallucination dropped
        assert done.claims[0]["claim_text"] == "30% relapse"
        assert done.claims[0]["claim_hash"]  # hash computed server-side
        assert done.input_tokens == 10 and done.output_tokens == 5
        assert done.evaluated_draft_sha  # staleness marker captured


@pytest.mark.asyncio
async def test_empty_submission_completes_with_no_rows(tmp_path, pg_session_factory, _settings_env):
    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        job = _running_job(s, ws.id)
        s.commit()
        ws_id, job_id = ws.id, job.id
    _seed_repo(tmp_path, ws_id)

    class _Adapter(FakeAgentAdapter):
        async def send_turn(self, handle, user_message):
            with pg_session_factory() as s:
                s.get(UncitedClaimsJob, job_id).claims = []
                s.commit()
            yield TurnDoneEvent(tokens_in=1, tokens_out=1, model="m", session_id="s")

    await run_uncited_claims_pass(
        job_id=job_id, claimed_generation=0, session_factory=pg_session_factory, settings=settings,
        adapter_builder=lambda **kw: _Adapter(),
    )
    with pg_session_factory() as s:
        done = s.get(UncitedClaimsJob, job_id)
        assert done.status == "completed" and done.claims == []


@pytest.mark.asyncio
async def test_no_submission_fails(tmp_path, pg_session_factory, _settings_env):
    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        job = _running_job(s, ws.id)
        s.commit()
        ws_id, job_id = ws.id, job.id
    _seed_repo(tmp_path, ws_id)

    fake = FakeAgentAdapter()
    fake.script_next_turn([TurnDoneEvent(tokens_in=1, tokens_out=1, model="m", session_id="s")])
    await run_uncited_claims_pass(
        job_id=job_id, claimed_generation=0, session_factory=pg_session_factory, settings=settings,
        adapter_builder=lambda **kw: fake,
    )
    with pg_session_factory() as s:
        job = s.get(UncitedClaimsJob, job_id)
        assert job.status == "failed" and "no result" in (job.error or "")


@pytest.mark.asyncio
async def test_late_finalize_records_tokens_without_resurrecting(
    tmp_path, pg_session_factory, _settings_env
):
    # spec §3: if the reaper/cancel failed the job between the turn and finalize, DON'T
    # resurrect it — but still record the tokens it spent so the daily cap stays honest.
    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        job = _running_job(s, ws.id)
        s.commit()
        ws_id, job_id = ws.id, job.id
    _seed_repo(tmp_path, ws_id)

    class _Adapter(FakeAgentAdapter):
        async def send_turn(self, handle, user_message):
            # simulate the reaper/cancel finalizing the job DURING the turn, after spend
            with pg_session_factory() as s:
                j = s.get(UncitedClaimsJob, job_id)
                j.claims = [{"claim_text": "30% relapse", "section_index": 1, "reason": "r"}]
                j.status = "failed"
                j.error = "timed out"
                s.commit()
            yield TurnDoneEvent(tokens_in=42, tokens_out=7, model="m", session_id="s")

    await run_uncited_claims_pass(
        job_id=job_id, claimed_generation=0, session_factory=pg_session_factory, settings=settings,
        adapter_builder=lambda **kw: _Adapter(),
    )
    with pg_session_factory() as s:
        job = s.get(UncitedClaimsJob, job_id)
        assert job.status == "failed"  # not resurrected to completed
        assert job.input_tokens == 42 and job.output_tokens == 7  # late spend still counted


@pytest.mark.asyncio
async def test_timeout_on_launch_fails(tmp_path, pg_session_factory, _settings_env):
    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    settings.uncited_claims_pass_timeout_seconds = 0.1
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        job = _running_job(s, ws.id)
        s.commit()
        ws_id, job_id = ws.id, job.id
    _seed_repo(tmp_path, ws_id)

    class _HangLaunch(FakeAgentAdapter):
        async def launch(self, context):
            await asyncio.sleep(2)
            return await super().launch(context)

    await run_uncited_claims_pass(
        job_id=job_id, claimed_generation=0, session_factory=pg_session_factory, settings=settings,
        adapter_builder=lambda **kw: _HangLaunch(),
    )
    with pg_session_factory() as s:
        assert s.get(UncitedClaimsJob, job_id).status == "failed"


@pytest.mark.asyncio
async def test_cancelled_before_launch_skips_the_llm(tmp_path, pg_session_factory, _settings_env):
    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        job = _running_job(s, ws.id)
        s.commit()
        ws_id, job_id = ws.id, job.id
    _seed_repo(tmp_path, ws_id)

    fake = FakeAgentAdapter()

    def builder(**kw):
        with pg_session_factory() as s:
            j = s.get(UncitedClaimsJob, job_id)
            j.status = "failed"
            j.error = "workspace deleted"
            s.commit()
        return fake

    await run_uncited_claims_pass(
        job_id=job_id, claimed_generation=0, session_factory=pg_session_factory, settings=settings,
        adapter_builder=builder,
    )
    assert fake.launches == []  # pre-launch recheck short-circuited — no LLM spend
    with pg_session_factory() as s:
        assert s.get(UncitedClaimsJob, job_id).status == "failed"
