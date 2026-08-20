import uuid

import pytest

from call_manager.db.models import ReviewRunItem
from call_manager.proposal_assist.port_impl import build_ports
from call_manager.proposal_assist.tools.registry import ToolContext, snapshot_invocation
from call_manager.proposal_assist.tools.review_submit import submit_review_impl


def _ctx(session_factory, ws_id, run_id, item_id):
    return ToolContext(
        workspace_id=ws_id, repo_dir=None, session_factory=session_factory,
        settings=None, origin_kind="review_session", origin_job_id=run_id,
        review_run_item_id=item_id,
        # GC-13: snapshot_invocation(ctx).stamp() requires lifecycle_generation is not
        # None. The workspace server_default is 0; pin the ctx to match.
        lifecycle_generation=0,
    )


@pytest.mark.asyncio
async def test_submit_stores_markdown_once(review_run_with_item):
    session_factory, ws_id, run_id, item_id = review_run_with_item
    ctx = _ctx(session_factory, ws_id, run_id, item_id)
    out = await submit_review_impl(
        ctx,
        ports=build_ports(session_factory),
        invocation=snapshot_invocation(ctx),
        markdown="# Findings\n- x",
    )
    assert out == {"stored": True}
    with session_factory() as s:
        item = s.get(ReviewRunItem, item_id)
        assert item.raw_response_json == {"markdown": "# Findings\n- x"}
    # exactly-once
    ctx2 = _ctx(session_factory, ws_id, run_id, item_id)
    out2 = await submit_review_impl(
        ctx2,
        ports=build_ports(session_factory),
        invocation=snapshot_invocation(ctx2),
        markdown="again",
    )
    assert "error" in out2


@pytest.mark.asyncio
async def test_submit_rejects_empty(review_run_with_item):
    session_factory, ws_id, run_id, item_id = review_run_with_item
    ctx = _ctx(session_factory, ws_id, run_id, item_id)
    out = await submit_review_impl(
        ctx,
        ports=build_ports(session_factory),
        invocation=snapshot_invocation(ctx),
        markdown="  ",
    )
    assert "error" in out


@pytest.mark.asyncio
async def test_submit_rejects_foreign_workspace(review_run_with_item):
    # A ctx stamped with a DIFFERENT workspace_id than the run's must be rejected.
    session_factory, ws_id, run_id, item_id = review_run_with_item
    other_ws = uuid.uuid4()
    ctx = _ctx(session_factory, other_ws, run_id, item_id)
    out = await submit_review_impl(
        ctx,
        ports=build_ports(session_factory),
        invocation=snapshot_invocation(ctx),
        markdown="# x",
    )
    assert "error" in out
