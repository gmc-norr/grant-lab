import uuid

from grant_lab.config import Settings
from grant_lab.proposal_assist.coherence.agent import (
    build_keeper_adapter,
    keeper_allowed_tools,
)
from grant_lab.proposal_assist.drafting.contract import LaunchContext


def test_allowlist_excludes_spec_propose_when_locked():
    assert "propose_spec_edit" in keeper_allowed_tools(locked=False)
    assert "propose_edit" in keeper_allowed_tools(locked=False)
    assert "read_spec" in keeper_allowed_tools(locked=False)
    assert "propose_spec_edit" not in keeper_allowed_tools(locked=True)
    assert "propose_edit" in keeper_allowed_tools(locked=True)


def test_keeper_adapter_uses_lock_aware_allowlist(tmp_path, pg_session_factory, _settings_env):
    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    job_id = uuid.uuid4()
    ws_id = uuid.uuid4()
    adapter = build_keeper_adapter(
        settings=settings,
        session_factory=pg_session_factory,
        workspace_id=ws_id,
        requested_by=1,
        job_id=job_id,
        locked=True,
        lifecycle_generation=0,
    )
    kwargs = adapter._build_options_kwargs(LaunchContext(workspace_id=ws_id))
    assert "mcp__proposal_assist__propose_spec_edit" not in kwargs["allowed_tools"]
    assert "mcp__proposal_assist__propose_edit" in kwargs["allowed_tools"]
    # No resume → fresh session.
    assert "resume" not in kwargs
