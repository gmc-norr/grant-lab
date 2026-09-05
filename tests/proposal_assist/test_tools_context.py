from pathlib import Path

import pytest

from grant_lab.proposal_assist.port_impl import build_ports
from grant_lab.proposal_assist.tools.context import (
    get_call_details_impl,
    get_profile_impl,
)
from grant_lab.proposal_assist.tools.registry import ToolContext
from tests.proposal_assist.conftest import make_user, make_workspace


def _build_ctx(pg_session_factory, wsid):
    from grant_lab.config import Settings

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


@pytest.mark.asyncio
async def test_the_profile_snapshot_is_workspace_scoped_not_profile_scoped(
    pg_session_factory,
    _settings_env,
):
    """A workspace MEMBER reads the linked profile's description even when that
    profile is private and the member is not one of its viewers. INTENDED, and
    pinned here so nobody "fixes" it as a leak (PD-13, decided 2026-09-04).

    The reasoning: adding somebody to a workspace is meant to share what the
    workspace was built on, the way sharing a document shares what it quotes.
    The owner had to be able to read the profile to link it (create_workspace_api
    applies can_read_profile, PD-9) and then chose to add the member.

    Note what this tool does NOT have: a user. `ToolContext` carries a
    workspace_id and no actor at all, so the profile read rule is not merely
    unapplied here — it is inapplicable. Anyone tempted to add it should change
    the SHARING decision above, not this function, or `?profile=` deep links and
    the digest will start disagreeing with the workspace about the same profile.

    The rule that still holds: only members reach this tool. A stranger to the
    workspace gets nothing, because turns are gated on require_member.
    """
    from grant_lab.db.models import Profile, ProfileKind

    with pg_session_factory() as s:
        owner = make_user(s, email="profile-owner@umu.se")
        private = Profile(
            name="Unpublished direction",
            kind=ProfileKind.ORGANIZATIONAL_UNIT.value,
            description="A research direction its owner has NOT shared.",
            visibility="private",
            is_active=True,
            created_by=owner.id,
        )
        s.add(private)
        s.flush()
        ws = make_workspace(s)
        ws.profile_id = private.id
        # Phase 0 copies the description in; the copy is what the agent reads.
        ws.profile_desc_snapshot = private.description
        s.commit()
        wsid = ws.id

    ctx = _build_ctx(pg_session_factory, wsid)
    result = await get_profile_impl(ctx, ports=build_ports(pg_session_factory))

    assert result["linked"] is True
    assert result["description"] == "A research direction its owner has NOT shared."
