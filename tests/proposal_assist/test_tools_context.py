from pathlib import Path

import pytest

from call_manager.proposal_assist.port_impl import build_ports
from call_manager.proposal_assist.tools.context import (
    get_call_details_impl,
    get_profile_impl,
)
from call_manager.proposal_assist.tools.registry import ToolContext
from tests.proposal_assist.conftest import make_workspace


def _build_ctx(pg_session_factory, wsid):
    from call_manager.config import Settings

    return ToolContext(
        workspace_id=wsid,
        repo_dir=Path("/tmp/unused"),
        session_factory=pg_session_factory,
        settings=Settings.model_construct(),
    )


@pytest.mark.asyncio
async def test_get_call_details_returns_empty_when_snapshot_empty(
    pg_session_factory,
    _settings_env,
):
    """A fresh workspace with no Phase 0 snapshot returns {linked: false}."""
    with pg_session_factory() as s:
        ws = make_workspace(s)  # call_body_snapshot defaults to ""
        s.commit()
        wsid = ws.id
    ctx = _build_ctx(pg_session_factory, wsid)
    result = await get_call_details_impl(ctx, ports=build_ports(pg_session_factory))
    assert result == {"linked": False}


@pytest.mark.asyncio
async def test_get_call_details_returns_snapshot_when_populated(
    pg_session_factory,
    _settings_env,
):
    """When Phase 0 has populated the workspace snapshot, the tool
    returns it (NOT the live calls row — by design)."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        ws.call_body_snapshot = "Full call body captured at workspace creation."
        ws.rubric_criteria = [{"name": "Excellence", "weight": 0.5}]
        ws.rubric_funder_notes = "Late submissions rejected."
        s.commit()
        wsid = ws.id
    ctx = _build_ctx(pg_session_factory, wsid)
    result = await get_call_details_impl(ctx, ports=build_ports(pg_session_factory))
    assert result["linked"] is True
    assert "Full call body" in result["call_body"]
    assert result["rubric_funder_notes"] == "Late submissions rejected."
    assert result["rubric_criteria"][0]["name"] == "Excellence"


@pytest.mark.asyncio
async def test_get_profile_returns_empty_when_snapshot_empty(
    pg_session_factory,
    _settings_env,
):
    with pg_session_factory() as s:
        ws = make_workspace(s)  # profile_desc_snapshot defaults to ""
        s.commit()
        wsid = ws.id
    ctx = _build_ctx(pg_session_factory, wsid)
    result = await get_profile_impl(ctx, ports=build_ports(pg_session_factory))
    assert result == {"linked": False}


@pytest.mark.asyncio
async def test_get_profile_returns_snapshot_when_populated(
    pg_session_factory,
    _settings_env,
):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        ws.profile_desc_snapshot = "Group X researches Y disease using Z methods."
        s.commit()
        wsid = ws.id
    ctx = _build_ctx(pg_session_factory, wsid)
    result = await get_profile_impl(ctx, ports=build_ports(pg_session_factory))
    assert result["linked"] is True
    assert "Group X" in result["description"]
