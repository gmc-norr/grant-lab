"""Tests that set_turn_provenance stamps allowed_tool_names + turn_selection
onto the retained ToolContext (Task 5: per-turn allow-list wiring)."""

import uuid
from pathlib import Path

import pytest

from call_manager.proposal_assist.drafting.turn_gating import READ_ONLY_TOOLS


@pytest.fixture
def adapter_with_ctx():
    """Return (adapter, ctx, ws_id) — a ClaudeAgentSDKAdapter with a
    ToolContext already inserted into its _tool_context_sink, so that
    set_turn_provenance can find and stamp it."""
    from call_manager.proposal_assist.drafting.claude_adapter import ClaudeAgentSDKAdapter
    from call_manager.proposal_assist.tools.registry import ToolContext

    ws_id = uuid.uuid4()
    ctx = ToolContext(
        workspace_id=ws_id,
        repo_dir=Path("/tmp"),
        session_factory=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
    )
    adapter = ClaudeAgentSDKAdapter(
        model="test",
        workspaces_dir="/tmp",
        tools_factory=None,
    )
    # Manually insert the ctx so set_turn_provenance finds it.
    adapter._tool_context_sink[ws_id] = ctx
    return adapter, ctx, ws_id


@pytest.mark.asyncio
async def test_adapter_set_turn_provenance_stamps_allowlist(adapter_with_ctx):
    adapter, ctx, ws = adapter_with_ctx  # ctx is the retained ToolContext
    await adapter.set_turn_provenance(
        ws, triggering_turn_id=None, triggering_prompt=None, created_by=None,
        origin_session_id=None, allowed_tool_names=set(READ_ONLY_TOOLS),
        turn_selection={"anchor": "x", "from_offset": 0, "to_offset": 1, "section_index": 2},
    )
    assert ctx.allowed_tool_names == set(READ_ONLY_TOOLS)
    assert ctx.turn_selection["anchor"] == "x"


@pytest.mark.asyncio
async def test_adapter_clears_allowlist_for_plain_turn(adapter_with_ctx):
    adapter, ctx, ws = adapter_with_ctx
    ctx.allowed_tool_names = set(READ_ONLY_TOOLS)
    await adapter.set_turn_provenance(
        ws, triggering_turn_id=None, triggering_prompt=None, created_by=None,
        origin_session_id=None, allowed_tool_names=None, turn_selection=None,
    )
    assert ctx.allowed_tool_names is None  # full list restored for normal chat


# ---------------------------------------------------------------------------
# Change 1: controller → registry → FakeAgentAdapter forwarding chain
# ---------------------------------------------------------------------------
# Uses the SubprocessRegistry variant (preferred fallback): no DB fixture
# needed because set_turn_provenance never touches the DB — it only stamps
# the ToolContext via the adapter.  Constructing a full WorkspaceController
# over a FakeAgentAdapter also exercises the controller hop; both hops are
# covered in a single test.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_controller_forwards_gating_through_registry_to_fake():
    """controller.set_turn_provenance → registry.set_turn_provenance →
    FakeAgentAdapter.set_turn_provenance: both allowed_tool_names and
    turn_selection survive BOTH forwarding hops without being dropped."""
    from call_manager.proposal_assist.drafting.controller import WorkspaceController
    from call_manager.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
    from call_manager.proposal_assist.drafting.registry import SubprocessRegistry

    fake = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=fake, idle_seconds=600)
    # session_factory is stored but never called by set_turn_provenance.
    controller = WorkspaceController(registry=registry, session_factory=None)  # type: ignore[arg-type]

    ws_id = uuid.uuid4()
    selection = {"anchor": "x", "from_offset": 0, "to_offset": 1, "section_index": 2}

    await controller.set_turn_provenance(
        ws_id,
        triggering_turn_id=None,
        triggering_prompt=None,
        created_by=None,
        origin_session_id=None,
        allowed_tool_names={"read_proposal"},
        turn_selection=selection,
    )

    # Both new params survived both hops.
    assert len(fake.provenance_gating) == 1
    gating_ws, gating_tools, gating_sel = fake.provenance_gating[-1]
    assert gating_ws == ws_id
    assert gating_tools == {"read_proposal"}
    assert gating_sel == selection

    # The existing 5-tuple provenance record is still intact (no regression).
    assert len(fake.provenance) == 1
    prov_ws, prov_ttid, prov_prompt, prov_by, prov_sess = fake.provenance[-1]
    assert prov_ws == ws_id
    assert prov_ttid is None
    assert prov_by is None
