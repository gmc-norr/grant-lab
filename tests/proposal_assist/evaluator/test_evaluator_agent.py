import uuid

from call_manager.config import Settings
from call_manager.proposal_assist.evaluator.agent import (
    build_evaluator_adapter,
    evaluator_allowed_tools,
)


def test_allowlist_is_only_submit_scorecard():
    assert evaluator_allowed_tools() == ["submit_scorecard"]


def test_adapter_builds_with_pinned_section_counts(_settings_env):
    adapter = build_evaluator_adapter(
        settings=Settings(),
        session_factory=lambda: None,
        workspace_id=uuid.uuid4(),
        requested_by=None,
        job_id=uuid.uuid4(),
        draft_section_count=3,
        spec_section_count=2,
        rubric_criteria=[{"name": "Quality", "weight": 1.0}],
        lifecycle_generation=0,
    )
    assert adapter._allowed_tools == ["submit_scorecard"]
    # tools_factory stamps evaluator provenance + the pinned counts on the ToolContext.
    assert callable(adapter._tools_factory)
