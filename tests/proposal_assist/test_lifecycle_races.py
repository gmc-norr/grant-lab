"""End-to-end race tests for the workspace lifecycle generation mechanism.

Retention Slice C0 (spec 4.8). Five scenarios that each require the
generation-gated authority to be wired correctly. They are the slice's PROOF
that a superseded retirement tears NOTHING down and a stale reactivation
un-retires NOTHING.

1. Forward race:   a delete DB-commits (gen N) then stalls before its signal;
                   a reactivation lands fully (gen N+1, retired=False); the
                   live room + turns entry are created; the delayed retirement
                   arrives at gen N and is superseded. Those resources survive.
2. Reverse race:   a reactivation DB-commits (gen N) but stalls before its
                   apply; a delete lands fully (gen N+1, retired=True); the late
                   apply fires at gen N and is a no-op. The workspace stays
                   retired.
3. Post-timeout:   a retirement's coroutine is still running after the sync
                   barrier already returned to the route; a reactivation + its
                   resources land in that window; the retirement finally runs,
                   finds itself superseded, and tears nothing down.
4. Orphaned jobs:  Task 4's third test, driven through the REAL delete route.
                   The jobs hook's DB thread is parked and orphaned by its own
                   wait_for; a reactivation then commits a higher generation;
                   the orphan wakes and its generation-predicated SQL updates
                   ZERO rows. The queued setup job survives.
5. Isolation:      a transition on workspace A leaves workspace B's room and
                   registry entry untouched.

Each test asserts on OBSERVABLE SUBSYSTEM behaviour — has_room, socket.closed,
_entries membership, the DB row's status, acquire/claim raising WorkspaceRetired
— never merely on `applied_generation`. Reading the counter would pass even if a
guard still consulted a stale set, which is the whole class this slice removed.

The active (reactivation) side in every in-memory race uses the SAME primitives
C1's restore route will call — there is no test-only re-implementation of the
transition:

    with pg_session_factory() as s:
        gen_active = bump_lifecycle_generation(s, workspace_id=ws_id)  # SQL col+1
        s.get(Workspace, ws_id).soft_deleted_at = None
        s.commit()
    await lifecycle.apply(ws_id, generation=gen_active, retired=False)

bump_lifecycle_generation allocates the generation in the database (never from a
value read into Python), so a reactivation and a delete can never share one — the
staleness the whole mechanism detects.
"""
from __future__ import annotations

import asyncio
import threading
import uuid

import call_manager.proposal_assist.jobs_retirement as jr
from call_manager.db.models import Workspace, WorkspaceSetupJob
from call_manager.proposal_assist.collab.room import ClientState, RoomManager
from call_manager.proposal_assist.drafting.contract import WorkspaceRetired
from call_manager.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from call_manager.proposal_assist.drafting.registry import SubprocessRegistry
from call_manager.proposal_assist.lifecycle import WorkspaceLifecycle
from call_manager.proposal_assist.retirement import WorkspaceRetirement
from call_manager.workspaces import bump_lifecycle_generation
from tests.proposal_assist.conftest import make_workspace

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _empty_load():
    return []


class _FakeSocket:
    """Minimal WebSocket stand-in that records close() calls. `closed` is the
    RFC-6455 code it was closed with, or None if it was never closed — so a test
    can assert a superseded retirement never touched it."""

    def __init__(self):
        self.closed: int | None = None

    async def send_bytes(self, data: bytes) -> None:
        pass

    async def close(self, code: int = 1000) -> None:
        self.closed = code


def _noop_hook():
    """A recording-free async hook for the coordinator slots a given race does
    not exercise (jobs/passes). The coordinator requires all four names."""

    async def _h(workspace_id, generation):
        pass

    return _h


def _build_coordinator(lifecycle, mgr, reg, *, jobs_hook=None, passes_hook=None):
    """A coordinator whose collab/turns hooks are the REAL subsystem teardowns,
    so a superseded retirement's no-op is observed against real live state."""
    return WorkspaceRetirement(
        hooks={
            "collab": mgr.retire_workspace,
            "turns": reg.retire_workspace,
            "jobs": jobs_hook or _noop_hook(),
            "passes": passes_hook or _noop_hook(),
        },
        lifecycle=lifecycle,
    )


def _fresh_subsystems():
    """A fresh lifecycle authority + collab manager + turns registry + coordinator,
    all sharing the ONE lifecycle instance (as production wires them)."""
    lifecycle = WorkspaceLifecycle()
    mgr = RoomManager(lifecycle=lifecycle)
    reg = SubprocessRegistry(
        adapter=FakeAgentAdapter(), idle_seconds=600, lifecycle=lifecycle
    )
    coordinator = _build_coordinator(lifecycle, mgr, reg)
    return lifecycle, mgr, reg, coordinator


async def _install_room(mgr, ws, artifact="draft"):
    """Open a live collab room for `ws` and return (key, socket)."""
    key = (ws, artifact)
    sock = _FakeSocket()
    await mgr.acquire(
        key, load=_empty_load, client=sock,
        state=ClientState(user_id=1, can_write=True),
    )
    return key, sock


async def _install_registry_entry(reg, ws):
    """Install a live turns-registry entry for `ws` and return the handle."""
    am_launcher, _ = await reg.claim_launch_or_wait(ws)
    assert am_launcher is True
    handle = object()
    installed = await reg.fulfill_pending_launch(ws, handle, lock_session=None)
    assert installed is True
    return handle


def _seed_workspace(pg_session_factory) -> uuid.UUID:
    """Seed a real Workspace row (lifecycle_generation defaults to 0) and return
    its id, so the reactivation side can allocate a generation the way the real
    restore route will."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        return ws.id


async def _reactivate(pg_session_factory, lifecycle, ws_id) -> int:
    """The reactivation the way C1's restore route will do it: allocate the next
    generation in the DB, clear soft_deleted_at in the SAME transaction, commit,
    then apply the transition to the authority. Returns the generation allocated.

    Deliberately NOT a hand-rolled `apply(gen=2)`: bump_lifecycle_generation is
    the exact call C1 makes, so these races keep their meaning once the route
    exists."""
    with pg_session_factory() as s:
        gen_active = bump_lifecycle_generation(s, workspace_id=ws_id)
        s.get(Workspace, ws_id).soft_deleted_at = None
        s.commit()
    await lifecycle.apply(ws_id, generation=gen_active, retired=False)
    return gen_active


def _delete_commits(pg_session_factory, ws_id) -> int:
    """The DB half of a delete's generation allocation: bump the row's generation
    and stamp soft_deleted_at, in one committed transaction. Returns the delete's
    generation. (The real delete route does this inside soft_delete_workspace_db;
    for the in-memory races we only need the generation it allocates, not the
    job/turn cascade, so we allocate it the same way — in the DB, never in
    Python.)"""
    from datetime import UTC, datetime

    with pg_session_factory() as s:
        gen_del = bump_lifecycle_generation(s, workspace_id=ws_id)
        s.get(Workspace, ws_id).soft_deleted_at = datetime.now(UTC)
        s.commit()
    return gen_del


# ---------------------------------------------------------------------------
# Race 1: forward race
#   A delete commits (gen N) and stalls before its signal. A reactivation lands
#   FULLY (gen N+1, retired=False) via the real primitives. A room + a turns
#   entry are then created. The delete's signal finally fires — coordinator.retire
#   at gen N. gen N <= N+1, so the transition is superseded and NO hook runs.
#   The room, the socket and the entry must all survive.
# ---------------------------------------------------------------------------


async def test_forward_race_resources_survive(pg_session_factory):
    lifecycle, mgr, reg, coordinator = _fresh_subsystems()
    ws_id = _seed_workspace(pg_session_factory)

    # DELETE side: allocate + commit its generation (row now soft-deleted), but
    # its signal has NOT reached the coordinator yet.
    gen_del = _delete_commits(pg_session_factory, ws_id)

    # REACTIVATION (the real primitives): a HIGHER generation, retired=False.
    gen_active = await _reactivate(pg_session_factory, lifecycle, ws_id)
    assert gen_active > gen_del

    # Live resources created AFTER the reactivation.
    key, sock = await _install_room(mgr, ws_id)
    await _install_registry_entry(reg, ws_id)
    assert mgr.has_room(key)
    assert ws_id in reg._entries

    # The delete's delayed signal arrives: superseded (gen_del <= gen_active).
    await coordinator.retire(ws_id, gen_del)

    # Nothing ran. THESE resources — the ones created after the reactivation —
    # are untouched.
    assert mgr.has_room(key), "room must survive a superseded retirement"
    assert sock.closed is None, "socket must not be closed by a superseded retirement"
    assert ws_id in reg._entries, "registry entry must survive a superseded retirement"


# ---------------------------------------------------------------------------
# Race 2: reverse race
#   A reactivation commits its generation (gen N) but its apply is stalled. A
#   delete then lands FULLY: it commits a higher generation (gen N+1) and its
#   coordinator.retire retires the workspace. The stalled reactivation's apply
#   finally fires at gen N — a no-op (N < N+1). The workspace must stay retired:
#   acquire and claim must still refuse.
# ---------------------------------------------------------------------------


async def test_reverse_race_workspace_stays_retired(pg_session_factory):
    lifecycle, mgr, reg, coordinator = _fresh_subsystems()
    ws_id = _seed_workspace(pg_session_factory)

    # REACTIVATION: allocate + commit its generation and clear soft_deleted_at —
    # but its apply is deliberately deferred (the reverse-race window).
    with pg_session_factory() as s:
        gen_active = bump_lifecycle_generation(s, workspace_id=ws_id)
        s.get(Workspace, ws_id).soft_deleted_at = None
        s.commit()

    # DELETE: a HIGHER generation, fully retired via the coordinator.
    gen_del = _delete_commits(pg_session_factory, ws_id)
    assert gen_del > gen_active
    await coordinator.retire(ws_id, gen_del)

    # The workspace is retired — a fresh room acquire refuses.
    with_retired = ClientState(user_id=1, can_write=True)
    try:
        await mgr.acquire(
            (ws_id, "draft"), load=_empty_load, client=_FakeSocket(), state=with_retired
        )
        raise AssertionError("acquire must raise WorkspaceRetired after the delete")
    except WorkspaceRetired:
        pass

    # The stalled reactivation's apply finally fires: gen_active < gen_del → no-op.
    await lifecycle.apply(ws_id, generation=gen_active, retired=False)

    # The workspace must STILL be retired — asserted on subsystem refusals, not
    # on the counter: a fresh acquire and a fresh launch claim must both refuse.
    try:
        await mgr.acquire(
            (ws_id, "draft"), load=_empty_load, client=_FakeSocket(), state=with_retired
        )
        raise AssertionError("stale reactivation un-retired the collab room")
    except WorkspaceRetired:
        pass
    try:
        await reg.claim_launch_or_wait(ws_id)
        raise AssertionError("stale reactivation un-retired the turns registry")
    except WorkspaceRetired:
        pass


# ---------------------------------------------------------------------------
# Race 3: post-timeout
#   signal_workspace_retirement's sync barrier returns to the route while the
#   retirement coroutine is still on the loop, unrun. We model that window with a
#   gate the retirement coroutine awaits before it calls coordinator.retire: the
#   route has "returned" (barrier timed out), a reactivation + its resources land,
#   and only THEN does the deferred retirement run — finds gen N <= N+1, tears
#   nothing down. Same survival assertion as race 1, reached the post-timeout way.
# ---------------------------------------------------------------------------


async def test_post_timeout_resources_survive(pg_session_factory):
    lifecycle, mgr, reg, coordinator = _fresh_subsystems()
    ws_id = _seed_workspace(pg_session_factory)

    gen_del = _delete_commits(pg_session_factory, ws_id)

    # The retirement coroutine is dispatched onto the loop but held before it can
    # reach coordinator.retire — this is the "barrier already returned, coroutine
    # still pending" window that signal_workspace_retirement leaves behind.
    barrier_returned = asyncio.Event()

    async def _deferred_retire():
        await barrier_returned.wait()
        await coordinator.retire(ws_id, gen_del)

    retire_task = asyncio.ensure_future(_deferred_retire())

    # In that window the reactivation lands fully and its live resources appear.
    gen_active = await _reactivate(pg_session_factory, lifecycle, ws_id)
    assert gen_active > gen_del
    key, sock = await _install_room(mgr, ws_id)
    await _install_registry_entry(reg, ws_id)
    assert mgr.has_room(key)
    assert ws_id in reg._entries

    # Now let the deferred retirement run. It finds gen_del superseded → no hook.
    barrier_returned.set()
    await asyncio.wait_for(retire_task, timeout=5)

    assert mgr.has_room(key), "room must survive a post-timeout superseded retirement"
    assert sock.closed is None, "socket must not be closed by a post-timeout retirement"
    assert ws_id in reg._entries, "entry must survive a post-timeout superseded retirement"


# ---------------------------------------------------------------------------
# Race 4: orphaned jobs thread, THROUGH THE REAL DELETE ROUTE
#   Task 4's third test, joined to the real route. Seed a workspace + queued
#   setup job. Park the jobs hook's DB thread (monkeypatch cancel_setup_jobs_for_
#   workspace to block). Call DELETE /api/workspaces/{id}: it commits the delete
#   at gen 1, dispatches coordinator.retire onto the app loop; the jobs hook's own
#   wait_for (1s) fires and ORPHANS the parked thread, so the route returns 204.
#   A reactivation then commits gen 2 in the DB. Release the orphan: its SQL runs
#   `WHERE lifecycle_generation = 1` against a row at 2 → ZERO rows. The queued
#   setup job survives.
#
#   This is a SYNC test: it drives the real route via the TestClient (whose ASGI
#   app runs on its OWN loop, app.state.app_loop), and drains THAT loop's default
#   executor to include the orphan thread's COMMIT before asserting.
# ---------------------------------------------------------------------------


def test_orphaned_jobs_thread_through_the_real_route_changes_nothing(
    client_authed_owner, current_user, pg_session_factory, monkeypatch
):
    # Seed a workspace owned by the request user, with a queued setup job. Its
    # lifecycle_generation starts at 0; the delete route will bump it to 1.
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="queued"))
        s.commit()
        ws_id = ws.id

    def _status():
        with pg_session_factory() as s:
            return s.query(WorkspaceSetupJob).filter_by(workspace_id=ws_id).one().status

    # Park the jobs hook's DB thread. The hook references the module-level name
    # jr.cancel_setup_jobs_for_workspace inside its worker, so patching it here is
    # what the thread actually calls. The wrapper runs the REAL cancel after the
    # release, then commits and signals `committed` — a deterministic post-commit
    # boundary the assertion can wait on WITHOUT shutting down the app loop's
    # executor (the TestClient's own lifespan teardown still needs it). The hook's
    # trailing session.commit() is then a harmless no-op on an empty transaction.
    entered = threading.Event()
    release = threading.Event()
    committed = threading.Event()
    real = jr.cancel_setup_jobs_for_workspace

    def _blocking(session, workspace_id, generation):
        entered.set()
        release.wait(timeout=30)  # bounded, so a failing test cannot hang the suite
        try:
            n = real(session, workspace_id, generation)
            session.commit()  # make the orphan's write (or its ZERO-row no-op) durable
            return n
        finally:
            committed.set()

    monkeypatch.setattr(jr, "cancel_setup_jobs_for_workspace", _blocking)

    try:
        # The real delete route: DB commits gen 1, then signal_workspace_retirement
        # dispatches coordinator.retire onto app_loop and blocks on the barrier. The
        # jobs hook parks; its wait_for (1s) fires, ORPHANING the parked thread; the
        # coordinator finishes; the barrier returns → 204.
        resp = client_authed_owner.delete(f"/api/workspaces/{ws_id}")
        assert resp.status_code == 204
        assert entered.is_set(), "the jobs hook's DB thread never parked — test proves nothing"

        with pg_session_factory() as s:
            assert s.get(Workspace, ws_id).soft_deleted_at is not None  # delete committed
            assert s.get(Workspace, ws_id).lifecycle_generation == 1  # gen the delete allocated

        # REACTIVATION: allocate gen 2 in the DB and clear soft_deleted_at (the real
        # primitive), so the orphan's `WHERE lifecycle_generation = 1` no longer matches.
        with pg_session_factory() as s:
            gen_active = bump_lifecycle_generation(s, workspace_id=ws_id)
            s.get(Workspace, ws_id).soft_deleted_at = None
            s.commit()
        assert gen_active == 2

        # Release the orphaned thread and wait for its COMMIT to land before asserting.
        release.set()
        assert committed.wait(timeout=30), "the orphaned jobs thread never committed"
    finally:
        release.set()  # idempotent: never leave a parked thread on a failure path

    assert _status() == "queued", (
        "an orphaned retirement thread cancelled a setup job after a later "
        "reactivation had moved the generation on — the SQL generation predicate "
        "is not gating the cancel"
    )


# ---------------------------------------------------------------------------
# Race 5: per-workspace isolation
#   Retiring workspace A must leave workspace B's live room and registry entry
#   completely untouched. This held BEFORE this slice (both tombstone sets were
#   already keyed by workspace), so no C0 mutation can falsify it — it is a
#   scope-regression check that catches a FUTURE change making a transition
#   process-wide rather than per-workspace.
# ---------------------------------------------------------------------------


async def test_per_workspace_isolation():
    lifecycle, mgr, reg, coordinator = _fresh_subsystems()
    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()

    # B's live resources, created before A is retired.
    key_b, sock_b = await _install_room(mgr, ws_b)
    await _install_registry_entry(reg, ws_b)

    # Give A real state to tear down, then retire A.
    key_a, _ = await _install_room(mgr, ws_a)
    await _install_registry_entry(reg, ws_a)
    await coordinator.retire(ws_a, 1)

    # A is torn down; B is entirely unaffected.
    assert not mgr.has_room(key_a), "A's room must be evicted by A's retirement"
    assert ws_a not in reg._entries, "A's registry entry must be torn down"
    assert mgr.has_room(key_b), "B's room must survive A's retirement"
    assert sock_b.closed is None, "B's socket must not be closed by A's retirement"
    assert ws_b in reg._entries, "B's registry entry must survive A's retirement"
