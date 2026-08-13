"""WIRING gates for Slice C1's central mechanism, against the PROCESS-GLOBAL
PASS_REGISTRY.

test_passes_registry.py builds a fresh `PassRegistry()` per test — correct for
unit isolation, but it means nothing there can observe whether the production
runners actually use the singleton the retirement hook retires. This file is the
other half: it drives the REAL runners and the REAL delete route, and it only
ever looks at `call_manager.proposal_assist.passes.PASS_REGISTRY`.

It closes two measured gaps:

  A. Four of the five `PASS_REGISTRY.track()` sites were unpinned. Replacing the
     singleton import with a module-local `PassRegistry()` in coherence,
     evaluator and uncited_claims plus `PassRegistry().track(...)` in the review
     parent left the whole C1 surface green — while a DELETE during a streaming
     pass would find an empty registry, return 0, and let the pass keep spending
     hosted Opus and writing rows into a deleted workspace.

  B. `_entry.bind_adapter(adapter)` had no coverage at any of its four sites.
     With `entry.adapter is None`, `Entry.open_generations()` returns 0 and
     `retire_adapter()` returns immediately (passes.py:65-72), so retirement
     degrades to a bare `task.cancel()` and the `_unconfirmed` transfer in
     `track()`'s finally can never fire — the one-shot adapter is dropped with a
     live subprocess attached. Deleting all four lines was green everywhere.
     passes.py:61-63 already states the standard these tests apply ("a defensive
     fallback would make deleting this method from the real adapter invisible to
     every test"); the fake adapters below therefore report a NON-EMPTY
     `unconfirmed_launch_ids()`, which every fake reachable from a runner in the
     existing suite hard-codes to `[]`.

Shape follows `test_workspaces_delete_route.py:216`
(`test_delete_reaches_the_turns_registry_tombstone`) — this project's precedent
for proving a retirement hook is really joined to a real registry, written after
the turns hook had this exact silent-unwiring failure.

SINGLETON HYGIENE: `pass_registry_isolation` (autouse) snapshots and restores
both `_live` and `_unconfirmed` around every test, because `_unconfirmed` is
deliberately persistent and one test's leftover entry is the next one's
spurious pass or failure.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import uuid

import pytest

from call_manager.config import Settings
from call_manager.db.models import (
    CoherenceJob,
    EvaluatorJob,
    ReviewRun,
    ReviewRunItem,
    UncitedClaimsJob,
)
from call_manager.proposal_assist.passes import PASS_REGISTRY, PassRegistry
from tests.proposal_assist.conftest import make_workspace


@pytest.fixture(autouse=True)
def pass_registry_isolation():
    """Snapshot + restore the process-global registry around each test."""
    live = {ws: set(entries) for ws, entries in PASS_REGISTRY._live.items()}
    unconfirmed = list(PASS_REGISTRY._unconfirmed)
    try:
        yield
    finally:
        PASS_REGISTRY._live = live
        PASS_REGISTRY._unconfirmed = unconfirmed


# --- fakes -------------------------------------------------------------------


class _StubAdapter:
    """The adapter surface a pass runner and the registry both use.

    `open_generations` > 0 makes this adapter LEAK: `unconfirmed_launch_ids()`
    stays non-empty even after `retire_unconfirmed()`, which is what drives the
    `_unconfirmed` transfer the bind_adapter tests observe.
    """

    def __init__(self, *, open_generations: int = 0):
        self._open = [uuid.uuid4() for _ in range(open_generations)]
        self.retire_calls = 0

    async def launch(self, context):
        handle = type("H", (), {})()
        handle.state = {"launch_id": uuid.uuid4()}
        handle.workspace_id = getattr(context, "workspace_id", None)
        return handle

    async def send_turn(self, handle, instruction):
        return
        yield  # pragma: no cover — makes this an async generator

    async def terminate(self, handle) -> None:
        return None

    def unconfirmed_launch_ids(self):
        return list(self._open)

    async def retire_unconfirmed(self) -> int:
        self.retire_calls += 1
        return len(self._open)  # deliberately does NOT confirm them dead


def _builder(*, open_generations: int, built: list):
    """An adapter_builder that records what it built, so a test can assert the
    entry is bound to THAT object."""

    def _build(**kwargs):
        adapter = _StubAdapter(open_generations=open_generations)
        built.append(adapter)
        return adapter

    return _build


# --- seeding -----------------------------------------------------------------

_SITES = ["coherence", "evaluator", "uncited", "review_parent"]


def _seed_repo(workspaces_dir, ws_id) -> None:
    repo = workspaces_dir / str(ws_id) / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "proposal.md").write_text("## Aims\n\nWe claim 30% relapse occurs.\n")
    (repo / "proposal-spec.md").write_text("## Aims\n\nSpec text.\n")


def _seed(site, pg_session_factory, tmp_path):
    """One 'running' row of the right kind on a fresh live workspace, plus its
    repo on disk. Returns (row_id, ws_id)."""
    with pg_session_factory() as s:
        ws = make_workspace(s, current_phase="active")
        ws.rubric_criteria = [
            {"name": "Scientific quality", "description": "Rigour", "weight": 1.0}
        ]
        ws.rubric_funder_notes = "Swedish university-hospital context."
        s.flush()
        if site == "coherence":
            row = CoherenceJob(workspace_id=ws.id, status="running")
        elif site == "evaluator":
            row = EvaluatorJob(workspace_id=ws.id, status="running")
        elif site == "uncited":
            row = UncitedClaimsJob(workspace_id=ws.id, status="running")
        else:  # review parent AND review child both hang off a running run
            row = ReviewRun(workspace_id=ws.id, status="running", draft_sha="0" * 64)
        s.add(row)
        s.flush()
        if site == "review_child":
            s.add(ReviewRunItem(review_run_id=row.id, kind="fit_check", status="queued"))
        s.commit()
        row_id, ws_id = row.id, ws.id
    _seed_repo(tmp_path, ws_id)
    return row_id, ws_id


def _module_for(site):
    if site == "coherence":
        from call_manager.proposal_assist.coherence import pass_runner as m

        return m
    if site == "evaluator":
        from call_manager.proposal_assist.evaluator import pass_runner as m

        return m
    if site == "uncited":
        from call_manager.proposal_assist.uncited_claims import pass_runner as m

        return m
    from call_manager.proposal_assist.review import pass_runner as m

    return m


def _invoke(site, *, row_id, ws_id, session_factory, settings, adapter_builder):
    """The coroutine that drives ONE track() site."""
    m = _module_for(site)
    common = {
        "session_factory": session_factory,
        "settings": settings,
        "adapter_builder": adapter_builder,
    }
    if site == "coherence":
        return m.run_coherence_pass(job_id=row_id, **common)
    if site == "evaluator":
        return m.run_evaluator_pass(job_id=row_id, **common)
    if site == "uncited":
        return m.run_uncited_claims_pass(job_id=row_id, **common)
    if site == "review_parent":
        return m.run_review_pass(run_id=row_id, **common)
    return m._run_one_call(
        kind="fit_check", instruction="x", run_id=row_id, workspace_id=ws_id, **common
    )


def _settings_for(tmp_path) -> Settings:
    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    return settings


# --- A. every track() site registers into the REAL singleton, BEFORE the prefix


@pytest.mark.parametrize("site", _SITES)
@pytest.mark.asyncio
async def test_a_pass_registers_in_the_real_singleton_before_its_prefix(
    site, pg_session_factory, tmp_path, monkeypatch, _settings_env
):
    """Two properties in one observation, both sampled from INSIDE the prefix.

    (1) the entry lives in the process-global PASS_REGISTRY — the object
        main.py's `"passes"` hook retires — not in some module-local registry,
        and (2) it is there ALREADY when the prefix begins.

    (2) is the rationale five source comments state verbatim ("an entry
        registered only after the prefix is invisible to a DELETE that commits
        during it") and nothing tested it: moving `track()` below the prefix was
        green.

    The sample is taken by wrapping `bounded_prefix_lock_waits`, which each
    runner calls exactly once, on the prefix's `with` line — so this pins the
    moment the prefix opens rather than a session-call ordinal.

    Mutations that must FAIL this: (a) swap `PASS_REGISTRY` for a module-local
    `PassRegistry()`; (b) move `async with PASS_REGISTRY.track(...)` below the
    prefix block.
    """
    row_id, ws_id = _seed(site, pg_session_factory, tmp_path)
    m = _module_for(site)

    at_prefix: list[int] = []
    real_bound = m.bounded_prefix_lock_waits

    def _sampling_bound(session):
        at_prefix.append(PASS_REGISTRY.live_count(ws_id))
        return real_bound(session)

    monkeypatch.setattr(m, "bounded_prefix_lock_waits", _sampling_bound)

    await _invoke(
        site,
        row_id=row_id,
        ws_id=ws_id,
        session_factory=pg_session_factory,
        settings=_settings_for(tmp_path),
        adapter_builder=_builder(open_generations=0, built=[]),
    )

    assert at_prefix, f"the {site} prefix never ran — the runner returned early"
    assert at_prefix[0] == 1, (
        f"the {site} prefix began with {at_prefix[0]} live entries in the "
        "process-global PASS_REGISTRY. Either the runner tracks into a different "
        "registry than the one main.py's retirement hook retires, or it registers "
        "AFTER the prefix — in both cases a DELETE committing during the prefix "
        "cancels nothing."
    )
    assert PASS_REGISTRY.live_count(ws_id) == 0, "the entry outlived the pass"


@pytest.mark.asyncio
async def test_the_review_child_registers_in_the_real_singleton_before_its_prefix(
    pg_session_factory, tmp_path, _settings_env
):
    """The fifth track() site. `_run_one_call` has no `bounded_prefix_lock_waits`
    — its prefix is the first session it opens — so the sample is taken by
    wrapping the session factory instead.

    Mutation: swap `PASS_REGISTRY` for a module-local `PassRegistry()` in
    review/pass_runner.py, or move the child's `track()` below its prefix
    session — either must FAIL here.
    """
    row_id, ws_id = _seed("review_child", pg_session_factory, tmp_path)
    samples: list[int] = []

    def _probing_factory():
        samples.append(PASS_REGISTRY.live_count(ws_id))
        return pg_session_factory()

    await _invoke(
        "review_child",
        row_id=row_id,
        ws_id=ws_id,
        session_factory=_probing_factory,
        settings=_settings_for(tmp_path),
        adapter_builder=_builder(open_generations=0, built=[]),
    )

    assert samples, "the child never opened its prefix session"
    assert samples[0] == 1, (
        f"the child's prefix session opened with {samples[0]} live entries in the "
        "process-global PASS_REGISTRY — it is either tracking elsewhere or "
        "registering after its prefix"
    )
    assert PASS_REGISTRY.live_count(ws_id) == 0


# --- B. bind_adapter, at all four sites that own an adapter -------------------
#
# The review PARENT is deliberately absent: it owns no adapter (every
# _run_one_call builds its own), which is why its track() has no `as _entry`.

_BIND_SITES = ["coherence", "evaluator", "uncited", "review_child"]


@pytest.mark.parametrize("site", _BIND_SITES)
@pytest.mark.asyncio
async def test_a_pass_binds_its_adapter_to_its_registry_entry(
    site, pg_session_factory, tmp_path, _settings_env
):
    """`_entry.bind_adapter(adapter)` is what makes retirement more than a bare
    `task.cancel()`.

    Observed through behaviour rather than by reading the attribute alone: the
    adapter below LEAKS (its `unconfirmed_launch_ids()` never empties), so on
    exit `track()`'s finally must transfer the entry to `_unconfirmed` for the
    reconcile tick to retry. With the bind deleted, `open_generations()` reads 0
    through the `self.adapter is None` branch, nothing transfers, and the
    subprocess is dropped un-reaped.

    Mutation: delete `_entry.bind_adapter(adapter)` — this must FAIL.
    """
    row_id, ws_id = _seed(site, pg_session_factory, tmp_path)
    built: list[_StubAdapter] = []
    before = PASS_REGISTRY.unconfirmed_count()

    await _invoke(
        site,
        row_id=row_id,
        ws_id=ws_id,
        session_factory=pg_session_factory,
        settings=_settings_for(tmp_path),
        adapter_builder=_builder(open_generations=1, built=built),
    )

    assert built, f"the {site} pass never built an adapter — it aborted before the bind"
    assert PASS_REGISTRY.unconfirmed_count() == before + 1, (
        f"the {site} pass exited with an adapter still holding an open generation, "
        "but nothing was transferred to _unconfirmed — its entry has no adapter "
        "bound, so retirement degrades to a bare task.cancel() and the reconcile "
        "tick can never repair the leak"
    )
    leaked = PASS_REGISTRY._unconfirmed[-1]
    assert leaked.workspace_id == ws_id
    assert leaked.adapter is built[-1], (
        "the transferred entry is not bound to the adapter this pass actually built"
    )


# --- C. main.py's hook, and the route that fires it --------------------------


def test_main_binds_the_passes_hook_to_the_process_global_registry(client_authed_owner):
    """An IDENTITY check on the hook's VALUE.

    test_retirement_coordinator.py only asserts the `"passes"` KEY exists (and
    that a missing key fails boot), so main.py could bind the hook to a throwaway
    `PassRegistry()` — or to anything else — and stay green.
    """
    hooks = client_authed_owner.app.state.workspace_retirement._hooks
    hook = hooks["passes"]
    assert getattr(hook, "__self__", None) is PASS_REGISTRY, (
        "the 'passes' retirement hook is not bound to the process-global "
        "PASS_REGISTRY that every pass runner tracks into"
    )
    assert getattr(hook, "__func__", None) is PassRegistry.retire_workspace


def test_delete_reaches_the_real_pass_registry(
    client_authed_owner, current_user, pg_session_factory
):
    """End-to-end for the PASSES half of the retirement fan-out: route ->
    signal_workspace_retirement -> coordinator -> PASS_REGISTRY.retire_workspace.

    The sibling of test_delete_reaches_the_turns_registry_tombstone. Every layer
    was covered in isolation (the registry against a throwaway instance, the
    coordinator with stub hooks, the route's order with the bridge stubbed) but
    nothing joined them, so replacing main.py's `"passes"` hook with a no-op left
    the suite green — and a silently unwired hook means deleting a workspace
    never stops its passes, which is the guarantee Slice C1 exists to deliver.

    Asserts the FUNCTIONAL teardown, not membership: the live task is really
    cancelled AND its adapter is really asked to retire. Driven on the app loop
    because the registry is async and this route test is sync.
    """
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id

    app = client_authed_owner.app
    adapter = _StubAdapter(open_generations=1)
    started = threading.Event()
    finished = threading.Event()

    async def _fake_pass() -> None:
        try:
            async with PASS_REGISTRY.track(ws_id) as entry:
                entry.bind_adapter(adapter)
                started.set()
                await asyncio.sleep(3600)
        finally:
            finished.set()

    fut = asyncio.run_coroutine_threadsafe(_fake_pass(), app.state.app_loop)
    try:
        assert started.wait(timeout=5), "the fake pass never started on the app loop"
        assert PASS_REGISTRY.live_count(ws_id) == 1  # precondition

        assert client_authed_owner.delete(f"/api/workspaces/{ws_id}").status_code == 204

        assert finished.wait(timeout=5), (
            "the in-flight pass was never cancelled — the delete did not reach "
            "PASS_REGISTRY.retire_workspace"
        )
        assert PASS_REGISTRY.live_count(ws_id) == 0
        assert adapter.retire_calls == 1, (
            "the pass's task was cancelled but its adapter was never retired — "
            "a live subprocess is left attached to a deleted workspace"
        )
    finally:
        fut.cancel()
        with contextlib.suppress(BaseException):
            fut.result(timeout=5)
