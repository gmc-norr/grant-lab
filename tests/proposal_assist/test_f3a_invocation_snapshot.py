"""F3a — the retained context is re-stamped every turn, and each tool
invocation gets an immutable snapshot of it."""

from __future__ import annotations

import uuid

import pytest

from call_manager.proposal_assist.tools.registry import ToolContext


def _ctx() -> ToolContext:
    return ToolContext(
        workspace_id=uuid.uuid4(),
        repo_dir=None,
        session_factory=None,
        settings=None,
    )


@pytest.mark.asyncio
async def test_two_turns_through_one_retained_context_carry_different_generations():
    """The retained subprocess and its ctx are reused across turns, so a stamp
    missed once leaks the previous turn's value. registry.py documents the same
    trap for provenance. No static check can see this; only a runtime one can."""
    from call_manager.proposal_assist.drafting.claude_adapter import ClaudeAgentSDKAdapter

    adapter = ClaudeAgentSDKAdapter.__new__(ClaudeAgentSDKAdapter)
    ctx = _ctx()
    adapter._tool_context_sink = {ctx.workspace_id: ctx}

    job_a, job_b = uuid.uuid4(), uuid.uuid4()
    await adapter.set_turn_provenance(
        ctx.workspace_id,
        triggering_turn_id=uuid.uuid4(),
        triggering_prompt="one",
        created_by=1,
        origin_session_id=None,
        lifecycle_generation=3,
        active_turn_job_id=job_a,
    )
    assert (ctx.lifecycle_generation, ctx.active_turn_job_id) == (3, job_a)

    await adapter.set_turn_provenance(
        ctx.workspace_id,
        triggering_turn_id=uuid.uuid4(),
        triggering_prompt="two",
        created_by=1,
        origin_session_id=None,
        lifecycle_generation=4,
        active_turn_job_id=job_b,
    )
    assert (ctx.lifecycle_generation, ctx.active_turn_job_id) == (4, job_b)


@pytest.mark.asyncio
async def test_the_stamp_is_unconditional_even_when_the_generation_repeats():
    """A conditional stamp leaks active_turn_job_id across turns whenever the
    generation happens NOT to change — which is the common case, since most
    turns run against a workspace nobody deleted."""
    from call_manager.proposal_assist.drafting.claude_adapter import ClaudeAgentSDKAdapter

    adapter = ClaudeAgentSDKAdapter.__new__(ClaudeAgentSDKAdapter)
    ctx = _ctx()
    adapter._tool_context_sink = {ctx.workspace_id: ctx}
    job_a, job_b = uuid.uuid4(), uuid.uuid4()

    for job_id in (job_a, job_b):
        await adapter.set_turn_provenance(
            ctx.workspace_id,
            triggering_turn_id=uuid.uuid4(),
            triggering_prompt="t",
            created_by=1,
            origin_session_id=None,
            lifecycle_generation=3,      # UNCHANGED across both turns
            active_turn_job_id=job_id,
        )

    assert ctx.active_turn_job_id == job_b, "the second turn's job id was dropped"


@pytest.mark.asyncio
async def test_an_in_flight_invocation_keeps_its_generation_across_a_restamp():
    """THE test for this slice. A tool blocked in run_in_threadpool must not
    observe a generation stamped after it started: its write would then be
    fenced perfectly against the WRONG epoch, which is worse than unfenced
    because it looks correct."""
    import asyncio

    from call_manager.proposal_assist.tools.registry import _safe_tool

    ctx = _ctx()
    ctx.lifecycle_generation = 3
    ctx.active_turn_job_id = uuid.uuid4()
    ctx.triggering_prompt = "turn one"

    entered = asyncio.Event()
    release = asyncio.Event()
    seen: dict = {}

    async def slow_impl(_ctx, *, invocation):
        entered.set()
        await release.wait()                 # the restamp happens in this gap
        seen["generation"] = invocation.lifecycle_generation
        seen["prompt"] = invocation.triggering_prompt
        return {"ok": True}

    task = asyncio.create_task(
        _safe_tool("read_proposal", ctx, slow_impl, ctx, pass_invocation=True)
    )
    await entered.wait()

    ctx.lifecycle_generation = 5             # what set_turn_provenance does
    ctx.triggering_prompt = "turn two"
    release.set()
    await task

    assert seen["generation"] == 3
    assert seen["prompt"] == "turn one", (
        "the invocation read provenance LATE and would stamp turn two's prompt "
        "onto a proposed change made during turn one"
    )


@pytest.mark.asyncio
async def test_the_snapshot_survives_run_in_threadpool():
    from starlette.concurrency import run_in_threadpool

    from call_manager.proposal_assist.tools.registry import _safe_tool

    ctx = _ctx()
    ctx.lifecycle_generation = 3
    seen: dict = {}

    async def impl(_ctx, *, invocation):
        def in_thread():
            return invocation.lifecycle_generation
        seen["generation"] = await run_in_threadpool(in_thread)
        return {"ok": True}

    await _safe_tool("read_proposal", ctx, impl, ctx, pass_invocation=True)
    assert seen["generation"] == 3


def test_the_snapshot_is_immune_to_a_mutation_of_the_SOURCE_selection():
    """`frozen=True` freezes the dataclass's own attributes, not the objects they
    point at. A plain dict here stays ALIASED to ctx.turn_selection, so mutating
    the source changes the supposedly immutable snapshot — and turn_selection is
    copied into durable proposed-change provenance (tools/proposal.py), so a late
    mutation misattributes stored data.

    This is safe today only because set_turn_provenance REPLACES the dict rather
    than mutating it. An immutability guarantee must not rest on that.
    """
    from call_manager.proposal_assist.tools.registry import snapshot_invocation

    ctx = _ctx()
    ctx.turn_selection = {"kind": "turn one", "nested": {"n": 1}}

    inv = snapshot_invocation(ctx)

    ctx.turn_selection["kind"] = "turn two"
    ctx.turn_selection["nested"]["n"] = 2

    assert inv.turn_selection["kind"] == "turn one"
    assert inv.turn_selection["nested"]["n"] == 1, "nested values must be frozen too"

    with pytest.raises(TypeError):
        inv.turn_selection["kind"] = "written through the snapshot"
