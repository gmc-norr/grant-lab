"""Scheduler-side gates for Slice C1."""

from __future__ import annotations

import concurrent.futures
import logging

import pytest

from call_manager import scheduler as scheduler_module


class _CancelledFuture:
    def result(self, timeout=None):
        raise concurrent.futures.CancelledError()


class _FailingFuture:
    def result(self, timeout=None):
        raise RuntimeError("boom")


def test_a_retired_pass_logs_as_retirement_not_as_a_tick_failure(caplog):
    """concurrent.futures.CancelledError IS an Exception subclass, so an
    `except Exception` around fut.result() reports a deliberate workspace delete as
    a crashed tick.

    Mutation: remove the `except concurrent.futures.CancelledError` branch.
    """
    with caplog.at_level(logging.INFO):
        scheduler_module._drain_pass_future(
            _CancelledFuture(), pass_name="coherence", timeout=1
        )

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], (
        "a deliberate retirement was logged at ERROR"
    )
    assert any("retired" in r.getMessage() for r in caplog.records)


def test_a_genuinely_failing_pass_is_still_reported(caplog):
    """The other half — the helper must not swallow real failures."""
    with caplog.at_level(logging.INFO):
        scheduler_module._drain_pass_future(
            _FailingFuture(), pass_name="coherence", timeout=1
        )
    assert [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.parametrize(
    "pass_name", ["coherence", "evaluator", "review", "uncited_claims"]
)
def test_every_pass_tick_drains_through_the_shared_helper(pass_name):
    """Testing _drain_pass_future alone proves nothing about the four ticks —
    deleting the call from three of them would stay green.

    Asserting on the source is deliberate: invoking each tick would need a live
    app_loop and a claimable job, which tests the scheduler's plumbing rather than
    this wiring. Mutation: restore the inline try/except in ANY ONE tick and its
    parameter case fails.
    """
    import inspect

    src = inspect.getsource(scheduler_module)
    start = src.index(f"def _{pass_name}_tick(")
    body = src[start : src.index("    sched.add_job(", start)]

    assert "_drain_pass_future(" in body, (
        f"_{pass_name}_tick does not drain through the shared helper"
    )
    # The helper now owns fut.result(); an inline `fut.result(...)` in the tick body
    # means the old try/except is back. (The `app_loop is None` fallback keeps its
    # own handler on purpose — it runs the coroutine directly, where a cancellation
    # is an asyncio.CancelledError and never reaches `except Exception` anyway.)
    assert "fut.result(" not in body, (
        f"_{pass_name}_tick drains its future inline instead of via the helper"
    )


def test_the_passes_reconcile_tick_is_a_sync_callable_not_a_coroutine_function():
    """This is a BackgroundScheduler running SYNC callables. Registering an
    `async def` directly hands it a coroutine nobody awaits — the unit test would
    pass while production reconciliation never runs.

    Mutation: register PASS_REGISTRY.reconcile_unconfirmed directly and this fails.
    """
    import asyncio
    import inspect

    src = inspect.getsource(scheduler_module)
    start = src.index('        id="passes_reconcile_tick"')
    reg = src[src.rindex("sched.add_job(", 0, start) : start]
    func_name = reg.split("sched.add_job(")[1].strip().split(",")[0].strip()

    assert func_name == "_passes_reconcile_tick", (
        f"the passes reconcile job is registered as {func_name!r}"
    )

    body_start = src.index(f"def {func_name}(")
    body = src[body_start : src.index("sched.add_job(", body_start)]
    assert not body.lstrip().startswith("async def"), "registered an async def"
    assert "run_coroutine_threadsafe" in body, (
        "the tick does not dispatch its coroutine onto app_loop"
    )
    assert not asyncio.iscoroutinefunction(
        getattr(scheduler_module, func_name, lambda: None)
    )


def test_the_passes_reconcile_tick_is_not_gated_on_drafting():
    """Hiding reconciliation behind drafting_enabled would strand every unconfirmed
    generation on a deployment with drafting off."""
    import inspect

    src = inspect.getsource(scheduler_module)
    start = src.index("def _passes_reconcile_tick(")
    body = src[start : src.index("sched.add_job(", start)]
    assert "drafting_enabled" not in body
    assert "anthropic_api_key" not in body
