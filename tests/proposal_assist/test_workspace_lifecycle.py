"""WorkspaceLifecycle: one authority, monotonic, order-independent.

Retention Slice C0 (spec 4.5). Correctness rests on monotonicity, NOT on the
lock: the jobs hook's work provably escapes every lock this process holds
(spec 4.6), so the mechanism has to be right when effects arrive late and out
of order.
"""

from __future__ import annotations

import asyncio
import uuid

from grant_lab.proposal_assist.lifecycle import WorkspaceLifecycle


def test_an_unknown_workspace_is_not_retired():
    lc = WorkspaceLifecycle()
    assert lc.is_retired(uuid.uuid4()) is False
    assert lc.applied_generation(uuid.uuid4()) == 0


async def test_a_transition_that_takes_effect_yields_true_and_sets_the_state():
    lc = WorkspaceLifecycle()
    ws = uuid.uuid4()
    async with lc.transition(ws, generation=1, retired=True) as took_effect:
        assert took_effect is True
    assert lc.is_retired(ws) is True
    assert lc.applied_generation(ws) == 1


async def test_a_superseded_transition_yields_false_and_changes_nothing():
    """The gate the coordinator branches on. Yielding False rather than raising
    is deliberate: a superseded retirement is a NORMAL outcome of two racing
    transitions, not an error, and the caller's job is to skip its work."""
    lc = WorkspaceLifecycle()
    ws = uuid.uuid4()
    await lc.apply(ws, generation=6, retired=False)
    async with lc.transition(ws, generation=5, retired=True) as took_effect:
        assert took_effect is False
    assert lc.is_retired(ws) is False
    assert lc.applied_generation(ws) == 6


async def test_a_lower_generation_is_ignored_in_both_directions():
    """The two races of spec 4.4, reduced to their essence. Neither side knows
    the other exists; monotonicity alone decides."""
    lc = WorkspaceLifecycle()
    ws = uuid.uuid4()

    # delete@5 arriving AFTER restore@6
    await lc.apply(ws, generation=6, retired=False)
    assert await lc.apply(ws, generation=5, retired=True) is False
    assert lc.is_retired(ws) is False

    # restore@6 arriving AFTER delete@7
    await lc.apply(ws, generation=7, retired=True)
    assert await lc.apply(ws, generation=6, retired=False) is False
    assert lc.is_retired(ws) is True
    assert lc.applied_generation(ws) == 7


async def test_the_same_generation_applied_twice_is_a_no_op():
    """Retries coalesce by construction (spec 5.4): N applies == 1 apply. `<=`,
    not `<`: if a repeat took effect, a duplicate delivery of one transition
    could flip state back after a later one had landed."""
    lc = WorkspaceLifecycle()
    ws = uuid.uuid4()
    assert await lc.apply(ws, generation=3, retired=True) is True
    assert await lc.apply(ws, generation=3, retired=False) is False
    assert lc.is_retired(ws) is True


async def test_state_is_per_workspace():
    lc = WorkspaceLifecycle()
    a, b = uuid.uuid4(), uuid.uuid4()
    await lc.apply(a, generation=1, retired=True)
    assert lc.is_retired(a) is True
    assert lc.is_retired(b) is False
    assert lc.applied_generation(b) == 0


async def test_concurrent_applies_converge_on_the_highest_generation():
    """Order-independence, exercised rather than asserted.

    The HIGHEST generation is delivered FIRST and the rest scramble behind it.
    Ascending delivery would be worthless here: last-write-wins would also end
    on 20, so the test would pass with monotonicity deleted.
    """
    lc = WorkspaceLifecycle()
    ws = uuid.uuid4()
    await lc.apply(ws, generation=20, retired=False)
    await asyncio.gather(
        *(lc.apply(ws, generation=g, retired=(g % 2 == 1)) for g in range(1, 20))
    )
    assert lc.applied_generation(ws) == 20, (
        "a superseded transition overwrote a newer one"
    )
    assert lc.is_retired(ws) is False   # generation 20 was retired=False


async def test_the_lock_is_held_for_the_whole_body_even_when_superseded():
    """Two properties in one exercise:

    1. Two transitions on the SAME workspace never overlap — this is what lets
       the coordinator run its teardown inside the body without a restore
       landing underneath it.
    2. The lock is held even for a SUPERSEDED transition. If it were released
       early on the stale path, a stale retirement could observe "superseded"
       and then interleave anyway.
    """
    lc = WorkspaceLifecycle()
    a, b = uuid.uuid4(), uuid.uuid4()
    order: list[str] = []

    async def hold(ws, tag, generation, delay):
        async with lc.transition(ws, generation=generation, retired=True):
            order.append(f"{tag}-in")
            await asyncio.sleep(delay)
            order.append(f"{tag}-out")

    # a2 carries the LOWER generation, so whichever order they arrive in, one of
    # the two bodies runs superseded.
    await asyncio.gather(
        hold(a, "a1", 9, 0.05), hold(a, "a2", 1, 0.0), hold(b, "b", 1, 0.0)
    )

    depth = 0
    for ev in [e for e in order if e.startswith("a")]:
        depth += 1 if ev.endswith("-in") else -1
        assert depth <= 1, f"two holders of the same workspace overlapped: {order}"
    assert order.count("b-in") == 1, "a sibling workspace was serialised behind it"
