import pytest

from call_manager.config import Settings
from call_manager.db.models import UncitedClaimsJob
from call_manager.proposal_assist.tools.registry import ToolContext
from call_manager.proposal_assist.tools.uncited_claims import submit_uncited_claims_impl
from tests.proposal_assist.conftest import make_workspace


def _ctx(tmp_path, session_factory, ws_id, job_id, *, draft_n=2):
    return ToolContext(
        workspace_id=ws_id,
        repo_dir=tmp_path,
        session_factory=session_factory,
        settings=Settings(),
        origin_kind="uncited_claims_session",
        origin_job_id=job_id,
        uncited_draft_section_count=draft_n,
    )


def _setup(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        job = UncitedClaimsJob(workspace_id=ws.id, status="running")
        s.add(job)
        s.commit()
        return ws.id, job.id


@pytest.mark.asyncio
async def test_valid_claims_are_written_raw(tmp_path, pg_session_factory, _settings_env):
    ws_id, job_id = _setup(pg_session_factory)
    ctx = _ctx(tmp_path, pg_session_factory, ws_id, job_id)
    res = await submit_uncited_claims_impl(
        ctx,
        claims=[
            {"claim_text": "30% of patients relapse", "section_index": 1, "reason": "statistic"},
            {"claim_text": "X outperforms Y", "section_index": 0, "reason": "comparative"},
        ],
    )
    assert res["stored"] is True and res["count"] == 2
    with pg_session_factory() as s:
        job = s.get(UncitedClaimsJob, job_id)
        # raw shape: no claim_hash yet (the runner adds it at finalize)
        assert job.claims[0]["claim_text"] == "30% of patients relapse"
        assert "claim_hash" not in job.claims[0]


@pytest.mark.asyncio
async def test_empty_claim_text_rejected(tmp_path, pg_session_factory, _settings_env):
    ws_id, job_id = _setup(pg_session_factory)
    ctx = _ctx(tmp_path, pg_session_factory, ws_id, job_id)
    res = await submit_uncited_claims_impl(
        ctx, claims=[{"claim_text": "  ", "section_index": 0, "reason": "x"}]
    )
    assert "error" in res


@pytest.mark.asyncio
async def test_section_index_out_of_range_rejected(tmp_path, pg_session_factory, _settings_env):
    ws_id, job_id = _setup(pg_session_factory)
    ctx = _ctx(tmp_path, pg_session_factory, ws_id, job_id, draft_n=1)
    res = await submit_uncited_claims_impl(
        ctx, claims=[{"claim_text": "a", "section_index": 5, "reason": "x"}]
    )
    assert "error" in res


@pytest.mark.asyncio
async def test_bool_section_index_rejected(tmp_path, pg_session_factory, _settings_env):
    ws_id, job_id = _setup(pg_session_factory)
    ctx = _ctx(tmp_path, pg_session_factory, ws_id, job_id)
    res = await submit_uncited_claims_impl(
        ctx, claims=[{"claim_text": "a", "section_index": True, "reason": "x"}]
    )
    assert "error" in res


@pytest.mark.asyncio
async def test_only_while_running(tmp_path, pg_session_factory, _settings_env):
    ws_id, job_id = _setup(pg_session_factory)
    with pg_session_factory() as s:
        s.get(UncitedClaimsJob, job_id).status = "failed"
        s.commit()
    ctx = _ctx(tmp_path, pg_session_factory, ws_id, job_id)
    res = await submit_uncited_claims_impl(
        ctx, claims=[{"claim_text": "a", "section_index": 0, "reason": "x"}]
    )
    assert "error" in res


@pytest.mark.asyncio
async def test_second_submit_rejected(tmp_path, pg_session_factory, _settings_env):
    ws_id, job_id = _setup(pg_session_factory)
    ctx = _ctx(tmp_path, pg_session_factory, ws_id, job_id)
    first = await submit_uncited_claims_impl(
        ctx, claims=[{"claim_text": "a", "section_index": 0, "reason": "first"}]
    )
    assert first["stored"] is True
    second = await submit_uncited_claims_impl(
        ctx, claims=[{"claim_text": "b", "section_index": 0, "reason": "second"}]
    )
    assert "error" in second
    with pg_session_factory() as s:
        assert s.get(UncitedClaimsJob, job_id).claims[0]["reason"] == "first"


@pytest.mark.asyncio
async def test_empty_claims_list_is_allowed(tmp_path, pg_session_factory, _settings_env):
    # zero uncited claims is a valid, healthy result — must store [] (not None), not error
    ws_id, job_id = _setup(pg_session_factory)
    ctx = _ctx(tmp_path, pg_session_factory, ws_id, job_id)
    res = await submit_uncited_claims_impl(ctx, claims=[])
    assert res["stored"] is True and res["count"] == 0
    with pg_session_factory() as s:
        assert s.get(UncitedClaimsJob, job_id).claims == []
