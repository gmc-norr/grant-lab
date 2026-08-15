"""Slice C3 — the mutex-held tail is bounded, and its timeout is a 429.

`acquire_commit_mutex` used to restore `lock_timeout` to DEFAULT (= disabled)
after winning the mutex, so every lock wait afterwards was unbounded WHILE the
transaction held the mutex every other writer for that workspace needs. This
file holds the gates for the fix.
"""

from __future__ import annotations

import functools
import threading

import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from call_manager.db.models import Workspace, WorkspaceSetupJob
from call_manager.proposal_assist.drafting.locking import (
    BUSY_ERROR_MESSAGE,
    acquire_commit_mutex,
    acquire_commit_mutex_live,
)
from tests.proposal_assist.conftest import make_workspace


class _Orig(Exception):
    """Stands in for psycopg's error object, which carries the SQLSTATE the
    handler keys on. The REAL 55P03 path is covered by the DB-backed gates
    below; this stub is only for the mapping itself."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__("canceling statement due to lock timeout")
        self.sqlstate = sqlstate


def _probe_app(app: FastAPI, sqlstate: str) -> None:
    @app.get("/api/_c3_probe")
    def _probe():  # pragma: no cover - body is the raise
        raise OperationalError("SELECT 1", {}, _Orig(sqlstate))


def test_a_lock_timeout_becomes_the_same_429_envelope_the_routes_emit(_app, client_authed_owner):
    """Byte-parity with the routes' own 429: code `too_many_requests`, the shared
    busy message, and Retry-After: 2."""
    _probe_app(_app, "55P03")
    resp = client_authed_owner.get("/api/_c3_probe")
    assert resp.status_code == 429, resp.text
    assert resp.headers.get("Retry-After") == "2"
    body = resp.json()
    assert body["error"]["code"] == "too_many_requests"
    assert body["error"]["message"] == BUSY_ERROR_MESSAGE


def test_a_non_lock_OperationalError_is_still_a_500(_app, client_authed_owner):
    """The handler must not swallow real database failures. 08006 is
    connection_failure — nothing to do with contention."""
    _probe_app(_app, "08006")
    resp = client_authed_owner.get("/api/_c3_probe")
    assert resp.status_code == 500, resp.text
    assert resp.json()["error"]["code"] == "internal_error"


def _hold_workspace_row(pg_session_factory, ws_id):
    """Hold the `workspaces` row FOR UPDATE taking NO mutex — the shape the four
    background pass prefixes really have (row_locks.py:71-77)."""
    holding, release = threading.Event(), threading.Event()

    def _holder():
        with pg_session_factory() as s:
            s.get(Workspace, ws_id, with_for_update=True)
            holding.set()
            release.wait(timeout=30)
            s.rollback()

    t = threading.Thread(target=_holder)
    t.start()
    assert holding.wait(timeout=5), "the holder never took the workspaces row"
    return release, t


def test_the_tail_after_the_mutex_is_bounded(pg_session_factory, current_user):
    """The core invariant. A transaction that has WON the mutex must not wait
    forever on a row: before this slice `lock_timeout` was restored to DEFAULT
    (disabled) at locking.py:207, so this call never returned.

    Asserted as "it raised", never as elapsed time — without the bound the
    statement does not return at all, so a hung thread IS the failure.
    """
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id

    release, t = _hold_workspace_row(pg_session_factory, ws_id)
    try:
        with pg_session_factory() as s:
            acquire_commit_mutex_live(s, ws_id)
            with pytest.raises(OperationalError) as exc:
                s.get(Workspace, ws_id, with_for_update=True)
            sqlstate = getattr(exc.value.orig, "sqlstate", None)
            assert sqlstate == "55P03", f"expected a lock timeout, got {sqlstate}"
            s.rollback()
    finally:
        release.set()
        t.join(timeout=30)


def test_a_nested_acquire_does_not_wipe_the_bound(pg_session_factory, current_user):
    """THE decisive gate. `checkpoint_artifact` re-acquires the same mutex
    mid-tail (kernel.py:202-204 documents the re-entrancy), and the old code
    re-ran `SET LOCAL lock_timeout = DEFAULT` there — erasing any bound its
    caller had established. Ten call sites reach checkpoint_artifact.

    If this test passes while `test_the_tail_after_the_mutex_is_bounded` also
    passes, the fix survives nesting. Mutation: restore the DEFAULT line and this
    must fail.
    """
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id

    release, t = _hold_workspace_row(pg_session_factory, ws_id)
    try:
        with pg_session_factory() as s:
            acquire_commit_mutex_live(s, ws_id)
            acquire_commit_mutex_live(s, ws_id)  # re-entrant, as checkpoint does
            with pytest.raises(OperationalError) as exc:
                s.get(Workspace, ws_id, with_for_update=True)
            assert getattr(exc.value.orig, "sqlstate", None) == "55P03"
            s.rollback()
    finally:
        release.set()
        t.join(timeout=30)


def test_acquire_commit_mutex_reapplies_the_tail_cap_after_the_advisory_lock():
    """The DB gates prove the tail is BOUNDED; this proves it is bounded at OUR
    constant, unconditionally, on the path that actually runs.

    Deleting the tail statement leaves both DB gates green, because the ACQUISITION
    `SET LOCAL lock_timeout` (10s) is issued BEFORE pg_advisory_xact_lock and
    persists for the transaction — a 10s wait still raises 55P03 inside the gates'
    30s holder window. And the ceilings test is static arithmetic over the constant,
    so it cannot notice the constant no longer being applied.

    Structural on purpose: it asserts the DECISION (a second SET LOCAL, after the
    lock, deriving from MUTEX_TAIL_LOCK_TIMEOUT_SECONDS) rather than a timing, so it
    cannot go flaky and cannot be satisfied by the acquisition cap standing in.

    Only scans TOP-LEVEL statements of the function body (`fn.body`), not every
    statement reachable via `ast.walk(fn)`. `ast.walk` descends into nested blocks,
    so it would find a tail statement moved into the `except OperationalError`
    handler and call the gate satisfied — even though that leaves the SUCCESS path
    with no tail cap at all, exactly the bug this gate exists to catch. Restricting
    to `fn.body` means what this proves is narrower and more honest: the tail cap
    is an unconditional top-level statement of the function — not nested in a
    branch, an exception handler, or reachable only after an early return — that
    runs on the success path, is positioned after the advisory-lock `try`, and
    derives from MUTEX_TAIL_LOCK_TIMEOUT_SECONDS.

    AST nodes only — no regex or substring scan over source text. Reading a parsed
    Constant's `.value` is node inspection; scanning the file's characters is not.
    """
    import ast
    import inspect

    from call_manager.proposal_assist.drafting import locking

    tree = ast.parse(inspect.getsource(locking))
    fn = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "acquire_commit_mutex"
    )

    def _literal_prefix(node):
        """The leading literal text of a Constant or JoinedStr argument."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            parts = []
            for v in node.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    parts.append(v.value)
                else:
                    break
            return "".join(parts)
        return ""

    set_locals = []
    for stmt in fn.body:
        if not isinstance(stmt, ast.Expr):
            continue
        node = stmt.value
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == "execute"):
            continue
        if not node.args:
            continue
        inner = node.args[0]
        if not (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Name)
            and inner.func.id == "text"
            and inner.args
        ):
            continue
        if _literal_prefix(inner.args[0]).startswith("SET LOCAL lock_timeout"):
            set_locals.append((node.lineno, inner.args[0]))

    assert len(set_locals) == 2, (
        f"expected exactly two `SET LOCAL lock_timeout` statements in "
        f"acquire_commit_mutex (the acquisition cap, then the tail cap), found "
        f"{len(set_locals)} at lines {[ln for ln, _ in set_locals]}. Deleting the "
        f"tail statement leaves every DB gate green — the 10s acquisition cap "
        f"stands in for it — so this count is the only thing guarding it. Note "
        f"this count can also drop to one for a reason that has nothing to do "
        f"with deletion: if the tail statement's SQL is hoisted behind a name "
        f"(e.g. a module constant passed to `text(...)`) rather than inlined as "
        f'a string literal at the call site, `_literal_prefix` returns "" for '
        f"it — an `ast.Name` is neither a Constant nor a JoinedStr — so it goes "
        f"uncounted here even though the statement still runs. If that's your "
        f"situation, inline the SQL at the call site or extend `_literal_prefix` "
        f"to resolve the name; the statement was never deleted."
    )

    try_node = next(n for n in fn.body if isinstance(n, ast.Try))
    (first_line, _), (tail_line, tail_arg) = sorted(set_locals)
    assert first_line < try_node.lineno < tail_line, (
        "the tail cap must be re-applied AFTER the advisory-lock try block, not "
        "before it — set before, it is just the acquisition cap under another name"
    )

    names = {n.id for n in ast.walk(tail_arg) if isinstance(n, ast.Name)}
    assert "MUTEX_TAIL_LOCK_TIMEOUT_SECONDS" in names, (
        f"the tail cap must derive from MUTEX_TAIL_LOCK_TIMEOUT_SECONDS, not from "
        f"{sorted(names) or 'a hardcoded literal'}. Deriving it from "
        f"timeout_seconds would silently make the tail cap follow each caller's "
        f"ACQUISITION budget (10s, or 5s at flip/hydrate) instead of the 2s the "
        f"ceilings test governs."
    )


def test_a_bounded_m1_failure_requeues_the_setup_job_rather_than_losing_it(
    pg_session_factory, current_user, tmp_path
):
    """Phase-0's _m1 takes the workspace row FOR UPDATE right after the mutex
    (setup.py:361-362). With the tail bounded that wait now RAISES instead of
    pinning the mutex — which is only safe if _fail requeues.

    Asserted on the job's STATUS, not on the exception: `_fail` sets
    status='queued' and started_at=None for ordinary contention, and only
    dead-letters at MAX_SETUP_ATTEMPTS. A test that merely asserts "it raised"
    would pass just as happily if the job were lost.
    """
    from call_manager.proposal_assist.phases import setup as setup_mod

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="queued"))
        s.commit()
        ws_id = ws.id

    with pg_session_factory() as s:
        job_id = s.scalar(
            select(WorkspaceSetupJob.id).where(WorkspaceSetupJob.workspace_id == ws_id)
        )

    # `client` is never reached: _m1 raises on the row wait, long before any LLM
    # call. The five fields are Phase0Worker's real signature (setup.py:314-318).
    worker = setup_mod.Phase0Worker(
        session_factory=pg_session_factory,
        client=None,
        extractor_version="v1",
        rubric_extractor_version="v1",
        workspaces_dir=str(tmp_path),
    )

    release, t = _hold_workspace_row(pg_session_factory, ws_id)
    try:
        # run_one() takes NO arguments — it claims the queued job itself
        # (setup.py:320-324) and routes any exception through _fail at :335.
        assert worker.run_one() is True, "the worker did not claim the queued job"
    finally:
        release.set()
        t.join(timeout=30)

    with pg_session_factory() as s:
        job = s.get(WorkspaceSetupJob, job_id)
    assert job.status == "queued", (
        f"a contended _m1 left the setup job in {job.status!r}; ordinary contention "
        f"must requeue (setup.py:633-634), not dead-letter"
    )
    assert job.attempts == 1
    assert job.started_at is None


def test_a_lock_timeout_is_not_on_any_kernel_clean_list():
    """The kernel's clean-error lists mean exactly one thing (kernel_accept.py:36-38):
    these errors are "raised BEFORE any fragment mutation", so the room doc is
    untouched and does NOT need evicting. Every other exception may have left
    room.doc mutated while the transaction rolled back, so it evicts.

    A tail lock timeout can fire on either side of that line — at _load_scoped's
    FOR UPDATE (clean) or at checkpoint/COMMIT (not) — and one exception type
    cannot distinguish them. Listing it as clean would skip eviction in the case
    where the doc WAS mutated and the transaction rolled back, diverging the live
    in-memory doc from the durable log.

    So the correct behaviour is to evict, and this gate exists to stop a future
    change from "tidying" the timeout onto any of the lists. It is deliberately
    structural: a room-driving test would prove today's behaviour, but this proves
    the DECISION, and it cannot rot or go flaky.

    THREE lists, not two. _CLEAN_INIT_SPEC_ERRORS is defined ten lines below
    _CLEAN_CITE_ERRORS and is easy to miss, but its except at collab_cite.py:142
    wraps a body that commits after mutating the doc — exactly as exposed.
    """
    from call_manager.proposal_assist.citations.collab_cite import (
        _CLEAN_CITE_ERRORS,
        _CLEAN_INIT_SPEC_ERRORS,
    )
    from call_manager.proposal_assist.proposals.kernel_accept import _CLEAN_ACCEPT_ERRORS

    for name, clean in (
        ("_CLEAN_ACCEPT_ERRORS", _CLEAN_ACCEPT_ERRORS),
        ("_CLEAN_CITE_ERRORS", _CLEAN_CITE_ERRORS),
        ("_CLEAN_INIT_SPEC_ERRORS", _CLEAN_INIT_SPEC_ERRORS),
    ):
        offenders = [e for e in clean if isinstance(e, type) and issubclass(OperationalError, e)]
        assert not offenders, (
            f"{name} lists {[e.__name__ for e in offenders]}, which would classify a "
            f"mutex-tail lock timeout as raised-before-mutation. It can also fire AFTER "
            f"the fragment mutation, and skipping the evict there leaves the live room "
            f"doc diverged from the durable log. See spec section 4.1."
        )


def test_hydration_really_can_raise_a_lock_timeout_after_the_mutex(
    pg_session_factory, current_user, tmp_path
):
    """The two close-code tests stub the raise, which proves the mapping but not
    that hydration can produce it. This drives the real path: load_or_hydrate takes
    the live mutex and THEN appends the seed (hydrate.py:55-60); that INSERT takes
    an FK parent lock on `workspaces`, which conflicts with a held FOR UPDATE. With
    the tail bounded the wait gives up after 2s with 55P03 instead of hanging.
    """
    from pathlib import Path

    from call_manager.proposal_assist.collab import state as collab_state
    from call_manager.proposal_assist.collab.hydrate import load_or_hydrate
    from call_manager.proposal_assist.drafting.locking import is_lock_timeout
    from tests.proposal_assist.conftest import init_workspace_repo

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, current_phase="active")
        s.commit()
        ws_id = ws.id

    init_workspace_repo(Path(tmp_path) / str(ws_id) / "repo", "## Background\n\nseed.\n")
    with pg_session_factory() as s:
        collab_state.enable_collab(s, workspace_id=ws_id, artifact="draft")
        s.commit()

    release, t = _hold_workspace_row(pg_session_factory, ws_id)
    try:
        with pg_session_factory() as s, pytest.raises(OperationalError) as exc:
            load_or_hydrate(s, workspace_id=ws_id, artifact="draft", workspaces_dir=str(tmp_path))
    finally:
        release.set()
        t.join(timeout=30)

    assert is_lock_timeout(exc.value), f"expected a 55P03 lock timeout, got {exc.value!r}"


def test_a_bounded_flip_commit_defers_instead_of_erroring_the_read(
    pg_session_factory, current_user
):
    """maybe_auto_enable is called on READ paths and its contract is to defer, not
    fail: on CommitMutexTimeout it returns False and stays legacy so the next read
    retries. Its enable_collab + commit run AFTER the mutex, so with the tail
    bounded that commit can raise 55P03 — and escaping to the 429 handler would
    defeat the defer arm. The row wait is real, not stubbed: the INSERT takes an FK
    parent lock on `workspaces`.
    """
    from call_manager.proposal_assist.collab import state as collab_state
    from call_manager.proposal_assist.collab.flip import maybe_auto_enable

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, current_phase="active")
        s.commit()
        ws_id = ws.id

    release, t = _hold_workspace_row(pg_session_factory, ws_id)
    try:
        with pg_session_factory() as s:
            assert maybe_auto_enable(s, workspace_id=ws_id, artifact="draft") is False
    finally:
        release.set()
        t.join(timeout=30)

    with pg_session_factory() as s:
        assert collab_state.is_collab_enabled(s, workspace_id=ws_id, artifact="draft") is False, (
            "the deferred flip committed anyway"
        )


def _wedge_mutex(pg_session_factory, ws_id):
    """Hold the workspace commit mutex until released, so the next acquirer times
    out. Returns (release_event, thread)."""
    holding = threading.Event()
    release = threading.Event()

    def _holder():
        with pg_session_factory() as s:
            acquire_commit_mutex(s, ws_id)
            holding.set()
            release.wait(timeout=30)
            s.rollback()

    t = threading.Thread(target=_holder)
    t.start()
    assert holding.wait(timeout=5), "the holder never acquired the mutex"
    return release, t


def test_the_collab_checkpoint_route_still_answers_503_not_the_new_429(
    client_authed_owner, current_user, pg_session_factory, monkeypatch
):
    """collab_checkpoint is the documented holdout: it maps CommitMutexTimeout to
    503, not 429. That is the mutex ACQUISITION failure and a different exception
    type, so Task 1's OperationalError handler must never see it — this asserts the
    two mappings did not collapse into one.

    Two things this test must get right, both of which a first draft got wrong.
    The route screens `is_collab_enabled` BEFORE it reaches the mutex and returns
    409 if the artifact is off (collab_checkpoint.py:43), so collab must be enabled
    or the test never exercises anything. And the route does NOT import
    acquire_commit_mutex_live — it reaches the mutex through checkpoint_artifact —
    so the patch target is the `collab.checkpoint` module, with raising=True so a
    future rename fails loudly instead of installing a dead attribute.
    """
    import call_manager.proposal_assist.collab.checkpoint as checkpoint_mod
    from call_manager.proposal_assist.collab import state as collab_state

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, current_phase="active")
        collab_state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        ws_id = ws.id

    # Shorten the acquisition wait from the production 10s. Pass NO call-time
    # keyword at the call site — a call-time keyword overrides a partial's, which is
    # how the consolidation slice's equivalent patch was silently defeated.
    monkeypatch.setattr(
        checkpoint_mod,
        "acquire_commit_mutex_live",
        functools.partial(checkpoint_mod.acquire_commit_mutex_live, timeout_seconds=1),
        raising=True,
    )

    release, t = _wedge_mutex(pg_session_factory, ws_id)
    try:
        resp = client_authed_owner.post(f"/api/workspaces/{ws_id}/collab/draft/checkpoint")
    finally:
        release.set()
        t.join(timeout=30)

    assert resp.status_code == 503, (
        f"expected the documented 503 holdout, got {resp.status_code}: {resp.text}"
    )
