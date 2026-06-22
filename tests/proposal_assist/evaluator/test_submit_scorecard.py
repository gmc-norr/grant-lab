import pytest

from call_manager.config import Settings
from call_manager.db.models import EvaluatorJob
from call_manager.proposal_assist.tools.registry import ToolContext
from call_manager.proposal_assist.tools.scorecard import submit_scorecard_impl
from tests.proposal_assist.conftest import make_workspace


def _ctx(tmp_path, session_factory, ws_id, job_id, *, draft_n=2, spec_n=1):
    return ToolContext(
        workspace_id=ws_id,
        repo_dir=tmp_path,
        session_factory=session_factory,
        settings=Settings(),
        origin_kind="evaluator_session",
        origin_job_id=job_id,
        eval_draft_section_count=draft_n,
        eval_spec_section_count=spec_n,
    )


def _setup(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        ws.rubric_criteria = [
            {"name": "Quality", "weight": 0.7},
            {"name": "Impact", "weight": 0.3},
        ]
        s.flush()
        job = EvaluatorJob(workspace_id=ws.id, status="running")
        s.add(job)
        s.commit()
        return ws.id, job.id


@pytest.mark.asyncio
async def test_valid_scorecard_is_written(tmp_path, pg_session_factory, _settings_env):
    ws_id, job_id = _setup(pg_session_factory)
    ctx = _ctx(tmp_path, pg_session_factory, ws_id, job_id)
    res = await submit_scorecard_impl(
        ctx,
        criteria=[
            {"index": 0, "name": "Quality", "band": "strong", "note": "Rigorous."},
            {
                "index": 1,
                "name": "Impact",
                "band": "gap",
                "note": "No KPIs.",
                "deep_link": {"artifact": "draft", "section_index": 1},
            },
        ],
        summary="Mostly there.",
    )
    assert res["stored"] is True
    with pg_session_factory() as s:
        job = s.get(EvaluatorJob, job_id)
        sc = job.scorecard
    # Impact is a gap at normalized effective weight 0.3 (>= 0.25) -> gap floor caps
    # overall at at_risk regardless of the weighted average.
    assert sc["overall_band"] == "at_risk"
    assert sc["criteria"][0]["effective_weight"] == 0.7
    assert sc["criteria"][1]["band"] == "gap"
    assert sc["summary"] == "Mostly there."


@pytest.mark.asyncio
async def test_missing_index_rejected(tmp_path, pg_session_factory, _settings_env):
    ws_id, job_id = _setup(pg_session_factory)
    ctx = _ctx(tmp_path, pg_session_factory, ws_id, job_id)
    res = await submit_scorecard_impl(
        ctx,
        criteria=[{"index": 0, "name": "Quality", "band": "strong", "note": "x"}],
        summary="",
    )
    assert "error" in res  # index 1 not covered


@pytest.mark.asyncio
async def test_name_mismatch_rejected(tmp_path, pg_session_factory, _settings_env):
    ws_id, job_id = _setup(pg_session_factory)
    ctx = _ctx(tmp_path, pg_session_factory, ws_id, job_id)
    res = await submit_scorecard_impl(
        ctx,
        criteria=[
            {"index": 0, "name": "WRONG", "band": "strong", "note": "x"},
            {"index": 1, "name": "Impact", "band": "adequate", "note": "y"},
        ],
        summary="",
    )
    assert "error" in res


@pytest.mark.asyncio
async def test_bad_band_and_out_of_range_deeplink_rejected(
    tmp_path, pg_session_factory, _settings_env
):
    ws_id, job_id = _setup(pg_session_factory)
    ctx = _ctx(tmp_path, pg_session_factory, ws_id, job_id, draft_n=1)
    bad_band = await submit_scorecard_impl(
        ctx,
        criteria=[
            {"index": 0, "name": "Quality", "band": "excellent", "note": "x"},
            {"index": 1, "name": "Impact", "band": "gap", "note": "y"},
        ],
        summary="",
    )
    assert "error" in bad_band
    bad_link = await submit_scorecard_impl(
        ctx,
        criteria=[
            {
                "index": 0,
                "name": "Quality",
                "band": "strong",
                "note": "x",
                "deep_link": {"artifact": "draft", "section_index": 5},
            },
            {"index": 1, "name": "Impact", "band": "gap", "note": "y"},
        ],
        summary="",
    )
    assert "error" in bad_link


@pytest.mark.asyncio
async def test_empty_rubric_accepts_empty_criteria(tmp_path, pg_session_factory, _settings_env):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        ws.rubric_criteria = []
        s.flush()
        job = EvaluatorJob(workspace_id=ws.id, status="running")
        s.add(job)
        s.commit()
        ws_id, job_id = ws.id, job.id
    ctx = _ctx(tmp_path, pg_session_factory, ws_id, job_id)
    res = await submit_scorecard_impl(ctx, criteria=[], summary="No criteria.")
    assert res["stored"] is True
    with pg_session_factory() as s:
        assert s.get(EvaluatorJob, job_id).scorecard["criteria"] == []
