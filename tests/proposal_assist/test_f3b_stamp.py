"""F3b — the stdlib-only foundation the tool layer is allowed to name."""

from __future__ import annotations

import uuid

import pytest

from call_manager.proposal_assist.ports.errors import (
    EffectRejected,
    UnfencedInvocation,
    WorkspaceGenerationStale,
)
from call_manager.proposal_assist.ports.stamp import FenceStamp, Provenance
from call_manager.proposal_assist.tools.registry import Invocation, ToolContext


def _ctx(**over) -> ToolContext:
    base = dict(
        workspace_id=uuid.uuid4(),
        repo_dir=None,
        session_factory=None,
        settings=None,
    )
    ctx = ToolContext(**base)
    for k, v in over.items():
        setattr(ctx, k, v)
    return ctx


def test_stamp_carries_the_four_identity_fields():
    turn_job = uuid.uuid4()
    origin_job = uuid.uuid4()
    ctx = _ctx(lifecycle_generation=7, active_turn_job_id=turn_job, origin_job_id=origin_job)
    stamp = Invocation.snapshot(ctx).stamp()
    assert stamp == FenceStamp(
        workspace_id=ctx.workspace_id,
        generation=7,
        active_turn_job_id=turn_job,
        origin_job_id=origin_job,
    )


def test_generation_zero_is_a_real_generation():
    """Workspace.lifecycle_generation has server_default 0, so a fresh workspace
    IS at generation 0. A falsy-check instead of an is-None check would refuse it."""
    stamp = Invocation.snapshot(_ctx(lifecycle_generation=0)).stamp()
    assert stamp.generation == 0


def test_an_invocation_without_a_claimed_generation_cannot_produce_a_stamp():
    """An actor that never claimed a generation has no belief to fence against.
    Refusing loudly beats defaulting to a generation it did not claim at."""
    ctx = _ctx(lifecycle_generation=None)
    with pytest.raises(UnfencedInvocation) as e:
        Invocation.snapshot(ctx).stamp()
    assert e.value.workspace_id == ctx.workspace_id


def test_the_stamp_is_frozen():
    stamp = Invocation.snapshot(_ctx(lifecycle_generation=1)).stamp()
    with pytest.raises(Exception):  # noqa: B017
        stamp.generation = 2  # type: ignore[misc]


def test_provenance_carries_the_row_data_the_stamp_deliberately_does_not():
    """origin_job_id appears in BOTH: the stamp uses it to target the actor's own
    job row, and provenance copies it into durable proposed-change rows."""
    turn_id, origin_job = uuid.uuid4(), uuid.uuid4()
    ctx = _ctx(
        lifecycle_generation=3,
        origin_kind="coherence_session",
        origin_session_id="sess-1",
        origin_job_id=origin_job,
        triggering_turn_id=turn_id,
        triggering_prompt="do the thing",
        created_by=42,
    )
    assert Invocation.snapshot(ctx).provenance() == Provenance(
        origin_kind="coherence_session",
        origin_session_id="sess-1",
        origin_job_id=origin_job,
        triggering_turn_id=turn_id,
        triggering_prompt="do the thing",
        created_by=42,
    )


def test_the_stale_error_still_imports_from_its_original_home():
    """fencing.py re-exports it so every pre-F3b import site keeps working."""
    from call_manager.proposal_assist.fencing import (
        WorkspaceGenerationStale as ReExported,
    )

    assert ReExported is WorkspaceGenerationStale


def test_effect_rejected_carries_the_message_the_tool_will_return():
    e = EffectRejected("a scorecard has already been submitted for this run")
    assert e.reason == "a scorecard has already been submitted for this run"
    assert str(e) == e.reason
