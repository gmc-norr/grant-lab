import pytest

from call_manager.proposal_assist.drafting.controller import WorkspaceController
from call_manager.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from call_manager.proposal_assist.drafting.registry import SubprocessRegistry
from tests.proposal_assist.conftest import make_workspace


@pytest.mark.asyncio
async def test_controller_launch_populates_resume_session_id_from_workspace(
    pg_session_factory,
):
    """When workspaces.last_sdk_session_id is set, controller.launch()
    passes it to the adapter via LaunchContext.resume_session_id —
    the B4 handoff for B3's native-resume scaffolding."""
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)
        ws.last_sdk_session_id = "previous-session-uuid"
        s.commit()
        wsid = ws.id

    await controller.launch(wsid)

    assert len(adapter.launches) == 1
    assert adapter.launches[0].resume_session_id == "previous-session-uuid"


@pytest.mark.asyncio
async def test_controller_launch_resume_session_id_is_none_for_fresh_workspace(
    pg_session_factory,
):
    """A workspace with no prior session yields LaunchContext.resume_session_id=None."""
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry,
        session_factory=pg_session_factory,
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id

    await controller.launch(wsid)
    assert adapter.launches[0].resume_session_id is None
