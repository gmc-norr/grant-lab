"""F3b — the two ORDERED LOCK CASES that prove the shared-lease protocol.

Not a straddle. Pausing an effect mid-flight and committing a lifecycle
transition in the gap deadlocks a CORRECT implementation: the paused effect
holds the shared lease and the transition needs the exclusive lock. What is
provable is which side wins the lock, in both directions.

Every ordering claim below rests on a hook that fires when the effect executes
its shared-lease statement. A hook that never fires MUST fail the test, never
silently skip its assertion: without that, an implementation that stopped taking
the lease at all turns both cases into slow tautologies — the observing thread
waits out its timeout, the effect has long since committed, and everything
passes (Task 5 review, findings 1 and 2).
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager

import pytest
from sqlalchemy import event, select, text

from call_manager.db.models import (
    EvaluatorJob,
    ProposedChange,
    ReviewRunItem,
    UncitedClaimsJob,
    Workspace,
)
from call_manager.proposal_assist.drafting.locking import acquire_commit_mutex
from call_manager.proposal_assist.port_impl.effects import SqlEffects
from call_manager.proposal_assist.ports.effects import (
    ProposeArtifactEdit,
    ProposeCitationInsert,
    SubmitReview,
    SubmitScorecard,
    SubmitUncitedClaims,
)
from call_manager.proposal_assist.ports.errors import WorkspaceGenerationStale
from call_manager.proposal_assist.ports.stamp import FenceStamp, Provenance
from tests.proposal_assist.f3b_seeds import (
    bump_generation,
    seed_evaluator_job,
    seed_review_item,
    seed_uncited_job,
    seed_workspace,
)

_G = 4  # the generation every case claims at
_NEXT = 5  # what the lifecycle transition moves it to

# The ONE string every hook in this file keys on: the SQL `acquire_shared_lease`
# emits. Named once so "fencing.py rewrote its statement" is a single visible
# edit here — and so that when it stops matching, every case fails loudly.
_LEASE_SQL = "pg_advisory_xact_lock_shared"

# Generous: every signal below fires in milliseconds when the protocol holds.
_SIGNAL_TIMEOUT = 10.0

# Deliberately BELOW `acquire_shared_lease`'s own 10s lock_timeout. If the lease
# hook never fires, the exclusive holder must let go before the blocked effect
# times out, so the failure surfaces here as "the hook never fired" rather than
# as a CommitMutexTimeout that says nothing about what broke.
_LEASE_ISSUE_TIMEOUT = 5.0

_PROV = Provenance(
    origin_kind="interactive_turn",
    origin_session_id=None,
    origin_job_id=None,
    triggering_turn_id=None,
    triggering_prompt=None,
    created_by=None,
)

EFFECT_NAMES = [
    "submit_scorecard",
    "submit_review",
    "submit_uncited_claims",
    "propose_artifact_edit",
    "propose_citation_insert",
]


def _in_thread(
    target: Callable[[], None], errors: dict[str, BaseException], role: str
) -> threading.Thread:
    """A thread whose exception is RECORDED rather than printed and lost.

    A bare `call()` in a worker resurfaces as `KeyError` on some observation
    dict in the main thread, which tells the reader nothing about what actually
    failed (Task 5 review, findings 4 and 5).
    """

    def _run() -> None:
        try:
            target()
        except Exception as exc:  # re-raised by _reraise, in the main thread
            errors[role] = exc

    return threading.Thread(target=_run, name=role)


def _reraise(errors: dict[str, BaseException]) -> None:
    """Surface a worker thread's exception before any observation is asserted."""
    for role, exc in errors.items():
        raise AssertionError(f"the {role} thread raised {exc!r}") from exc


def _proposed_change_landed(session, workspace_id, inserted: list[uuid.UUID]) -> bool:
    """Did the propose_* effect's row land?

    EXACT by the id the operation returned, once we have it — positional
    `count() == 1` would also accept somebody else's row. Two reasons for the
    fallback: in Case B the operation raises and returns no id at all, and in
    Case A the DB commit precedes the Python return, so the lifecycle thread can
    win the exclusive lock before `inserted` is populated. "Any row for this
    workspace" answers both, and it is what Case B has to rule out.
    """
    if inserted:
        return session.get(ProposedChange, inserted[0]) is not None
    return session.query(ProposedChange).filter_by(workspace_id=workspace_id).count() > 0


def _build_case(session, effects: SqlEffects, name: str):
    """Seed one effect's rows and return (workspace_id, call, landed).

    `call()` runs the operation. `landed(session)` reports whether its write is
    durably present — by FIELD, never by row count: three of these five update an
    existing row, so a stale write leaves the count unchanged.

    Inner defs rather than lambdas: `ruff` selects the E rules and E731 forbids
    assigning a lambda to a name.
    """
    ws_id = seed_workspace(session, generation=_G)

    if name == "submit_scorecard":
        job_id = seed_evaluator_job(session, ws_id, generation=_G)
        stamp = FenceStamp(
            workspace_id=ws_id,
            generation=_G,
            active_turn_job_id=None,
            origin_job_id=job_id,
        )

        def call():
            effects.submit_scorecard(stamp, SubmitScorecard(scorecard={"ok": 1}))

        def landed(s):
            return s.get(EvaluatorJob, job_id).scorecard is not None

    elif name == "submit_review":
        run_id, item_id = seed_review_item(session, ws_id, generation=_G)
        stamp = FenceStamp(
            workspace_id=ws_id,
            generation=_G,
            active_turn_job_id=None,
            origin_job_id=run_id,
        )

        def call():
            effects.submit_review(
                stamp, SubmitReview(review_run_item_id=item_id, markdown="body")
            )

        def landed(s):
            return s.get(ReviewRunItem, item_id).raw_response_json is not None

    elif name == "submit_uncited_claims":
        job_id = seed_uncited_job(session, ws_id, generation=_G)
        stamp = FenceStamp(
            workspace_id=ws_id,
            generation=_G,
            active_turn_job_id=None,
            origin_job_id=job_id,
        )

        def call():
            effects.submit_uncited_claims(stamp, SubmitUncitedClaims(claims=[]))

        def landed(s):
            return s.get(UncitedClaimsJob, job_id).claims is not None

    elif name == "propose_artifact_edit":
        stamp = FenceStamp(
            workspace_id=ws_id,
            generation=_G,
            active_turn_job_id=None,
            origin_job_id=None,
        )
        inserted: list[uuid.UUID] = []

        def call():
            inserted.append(
                effects.propose_artifact_edit(
                    stamp,
                    ProposeArtifactEdit(
                        target_artifact="draft",
                        section_index=0,
                        new_text="t",
                        base_commit_sha="a" * 40,
                        base_section_sha="b" * 64,
                        provenance=_PROV,
                    ),
                )
            )

        def landed(s):
            return _proposed_change_landed(s, ws_id, inserted)

    elif name == "propose_citation_insert":
        stamp = FenceStamp(
            workspace_id=ws_id,
            generation=_G,
            active_turn_job_id=None,
            origin_job_id=None,
        )
        inserted = []

        def call():
            inserted.append(
                effects.propose_citation_insert(
                    stamp,
                    ProposeCitationInsert(
                        section_index=0,
                        base_commit_sha="a" * 40,
                        base_section_sha="b" * 64,
                        reference_id=uuid.uuid4(),
                        anchor="x",
                        chunk_index=0,
                        evidence_quote="q",
                        stance="supports",
                        confidence=None,
                        provenance=_PROV,
                    ),
                )
            )

        def landed(s):
            return _proposed_change_landed(s, ws_id, inserted)

    else:  # pragma: no cover - guards a typo in EFFECT_NAMES
        raise AssertionError(f"unknown effect {name!r}")

    return ws_id, call, landed


@contextmanager
def _hold_the_lease_for(engine, seconds: float, signal: threading.Event):
    """Pause the effect's own thread immediately AFTER it wins the shared lease
    and while it still holds it, so the lifecycle transition provably queues
    behind it. Surgical: no production hook, and the pause is on the effect's
    connection inside its own transaction.

    `signal` is the ONLY evidence that this fired. Whoever waits on it must
    assert the wait's result — see the lifecycle thread in Case A.
    """

    def _hook(conn, cursor, statement, params, context, executemany):
        if _LEASE_SQL in statement:
            signal.set()
            time.sleep(seconds)

    event.listen(engine, "after_cursor_execute", _hook)
    try:
        yield
    finally:
        event.remove(engine, "after_cursor_execute", _hook)


@contextmanager
def _watch_the_lease(engine, marks: dict, issued: threading.Event):
    """Stamp WHEN the effect's shared-lease statement was issued and when it was
    granted, and signal the issue so the holder can commit into that window.

    Two stamps, because only the PAIR is an ordering fact. A statement issued
    BEFORE the transition's commit and granted AFTER it can only have been
    waiting on a lock the transition held; neither stamp is a duration, and a
    duration could not prove it (Task 5 review, finding 2).
    """

    def _before(conn, cursor, statement, params, context, executemany):
        if _LEASE_SQL in statement:
            marks.setdefault("lease_issued", time.monotonic())
            issued.set()

    def _after(conn, cursor, statement, params, context, executemany):
        if _LEASE_SQL in statement:
            marks["lease_granted"] = time.monotonic()

    event.listen(engine, "before_cursor_execute", _before)
    event.listen(engine, "after_cursor_execute", _after)
    try:
        yield
    finally:
        event.remove(engine, "before_cursor_execute", _before)
        event.remove(engine, "after_cursor_execute", _after)


@pytest.mark.parametrize("name", EFFECT_NAMES)
def test_case_a_effect_first_the_write_lands_and_lifecycle_waits(
    name, pg_session_factory, migrated_engine
):
    """CASE A. The effect wins the lease; the transition blocks until it commits.
    The write LANDS — correct, it was live when it ran. Without this case the
    suite would pass an implementation that simply refuses everything."""
    effects = SqlEffects(pg_session_factory)
    with pg_session_factory() as s:
        ws_id, call, landed = _build_case(s, effects, name)

    lease_held = threading.Event()
    observed: dict = {}
    errors: dict[str, BaseException] = {}

    def _effect():
        call()

    def _lifecycle():
        # CAPTURE the wait's result. Discarding it (as this test first did) makes
        # the whole case a tautology: with no lease the hook never fires, this
        # thread simply waits out the timeout, the effect committed long ago, and
        # every assertion below passes while proving nothing about ordering.
        observed["lease_signalled"] = lease_held.wait(timeout=_SIGNAL_TIMEOUT)
        with pg_session_factory() as s:
            acquire_commit_mutex(s, ws_id, timeout_seconds=20)
            # Ordering fact, not a timing artifact: we hold the EXCLUSIVE lock,
            # so if the effect's write is visible here it committed first.
            observed["effect_visible_when_lifecycle_won"] = landed(s)
            bump_generation(s, ws_id, to=_NEXT)
            s.commit()

    with _hold_the_lease_for(migrated_engine, 1.0, lease_held):
        threads = [
            _in_thread(_effect, errors, "effect"),
            _in_thread(_lifecycle, errors, "lifecycle"),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

    _reraise(errors)
    assert observed.get("lease_signalled") is True, (
        f"the effect never executed a {_LEASE_SQL!r} statement, so the lifecycle "
        "thread never queued behind a lease — it just waited out its timeout and "
        "this case proves nothing about lock ordering"
    )
    assert observed.get("effect_visible_when_lifecycle_won") is True
    with pg_session_factory() as s:
        assert landed(s) is True
        assert s.get(Workspace, ws_id).lifecycle_generation == _NEXT


@pytest.mark.parametrize("name", EFFECT_NAMES)
def test_case_b_lifecycle_first_the_effect_lands_nothing(
    name, pg_session_factory, migrated_engine
):
    """CASE B — the one that matters. The transition wins the exclusive lock; the
    effect blocks, then evaluates its predicate against the NEW generation and
    produces no semantic effect at all.

    "The effect blocked" is asserted as an ORDERING FACT, never as a duration. A
    duration cannot prove it: for `call()` to raise at all it must read the
    workspaces row AFTER the transition committed, so a long elapsed time holds
    whether the effect queued on the lease or merely started late. What only a
    blocked effect can produce is a lease statement ISSUED before the
    transition's commit and GRANTED after it.
    """
    effects = SqlEffects(pg_session_factory)
    with pg_session_factory() as s:
        ws_id, call, landed = _build_case(s, effects, name)

    marks: dict[str, float] = {}
    errors: dict[str, BaseException] = {}
    holder_ready = threading.Event()
    effect_starting = threading.Event()
    lease_issued = threading.Event()

    def _lifecycle():
        with pg_session_factory() as s:
            acquire_commit_mutex(s, ws_id, timeout_seconds=20)
            bump_generation(s, ws_id, to=_NEXT)
            holder_ready.set()
            effect_starting.wait(timeout=_SIGNAL_TIMEOUT)
            # Hold until the effect's lease request is OUTSTANDING, so the commit
            # below provably lands while the effect is queued behind it. Falls
            # through on timeout — the main thread asserts the hook fired.
            lease_issued.wait(timeout=_LEASE_ISSUE_TIMEOUT)
            time.sleep(0.5)  # let the issued statement reach the server and queue
            marks["lifecycle_commit"] = time.monotonic()
            s.commit()

    with _watch_the_lease(migrated_engine, marks, lease_issued):
        t = _in_thread(_lifecycle, errors, "lifecycle")
        t.start()
        try:
            assert holder_ready.wait(timeout=_SIGNAL_TIMEOUT), (
                "the lifecycle thread never took the exclusive mutex"
            )
            effect_starting.set()
            with pytest.raises(WorkspaceGenerationStale) as e:
                call()
        finally:
            t.join(timeout=60)

    _reraise(errors)
    assert "lease_issued" in marks, (
        f"the effect never executed a {_LEASE_SQL!r} statement — it took no "
        "shared lease at all, so nothing here shows it blocked on one"
    )
    assert "lease_granted" in marks, "the effect's lease statement never returned"
    assert "lifecycle_commit" in marks, "the lifecycle thread never reached its commit"
    assert marks["lease_issued"] < marks["lifecycle_commit"], (
        "the effect asked for the lease only AFTER the transition had committed"
    )
    assert marks["lease_granted"] > marks["lifecycle_commit"], (
        "the effect's shared lease was granted BEFORE the transition committed, "
        "so it never queued behind the exclusive lock"
    )
    assert (e.value.expected, e.value.actual) == (_G, _NEXT)
    with pg_session_factory() as s:
        assert landed(s) is False


def test_without_the_lease_a_generation_change_lands_a_stale_insert(pg_session_factory):
    """REGRESSION for the mechanism itself, not for any operation.

    live_at_generation is an exists() subquery taking NO lock on the workspace
    row, so under READ COMMITTED it evaluates against the statement's snapshot.
    An INSERT has no target row and therefore no re-check to mask that. Here the
    statement starts at generation G, a second connection commits G+2 while it
    sleeps, and the G-era row lands durably against a workspace now at G+2.

    This is WHY acquire_shared_lease exists. The same interleaving with the lease
    is Case B above, which lands nothing.

    The interleaving is ASSERTED, not assumed: on a loaded box the transition
    thread can stall past the pg_sleep and commit after the INSERT, at which
    point the row lands with no race at all and the demonstration demonstrates
    nothing (Task 5 review, finding 3).
    """
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=7)

    pc_id = uuid.uuid4()
    predicate_only = text(
        """
        INSERT INTO proposed_changes
            (id, workspace_id, target_artifact, section_index, base_commit_sha,
             base_section_sha, kind, payload, origin_kind)
        SELECT :pc, :ws, 'draft', 0, 'a', 'b', 'prose_edit',
               '{}'::jsonb, 'interactive_turn'
        FROM (SELECT pg_sleep(1.5)) AS _pause
        WHERE EXISTS (
            SELECT 1 FROM workspaces
            WHERE id = :ws AND soft_deleted_at IS NULL AND lifecycle_generation = :gen
        )
        """
    )

    marks: dict[str, float] = {}
    errors: dict[str, BaseException] = {}

    def _transition():
        time.sleep(0.4)  # inside the sleeping statement's lifetime
        with pg_session_factory() as s:
            bump_generation(s, ws_id, to=9)
            s.commit()
            # Stamped AFTER the commit returns: the moment the new generation
            # became visible to other connections is what has to fall inside the
            # sleeping statement's window.
            marks["transition_commit"] = time.monotonic()

    t = _in_thread(_transition, errors, "transition")
    t.start()
    with pg_session_factory() as s:
        t_start = time.monotonic()
        s.execute(predicate_only, {"pc": pc_id, "ws": ws_id, "gen": 7})
        t_end = time.monotonic()
        s.commit()
    t.join(timeout=30)

    _reraise(errors)
    assert "transition_commit" in marks, "the transition thread never committed"
    assert t_start < marks["transition_commit"] < t_end, (
        "the race did not reproduce: the transition must commit DURING the "
        "sleeping statement, not before or after it (commit at "
        f"{marks['transition_commit'] - t_start:+.3f}s, statement ran "
        f"{t_end - t_start:.3f}s)"
    )

    with pg_session_factory() as s:
        assert s.get(ProposedChange, pc_id) is not None, (
            "the G-era row did not land — the predicate-only race did not "
            "reproduce even though the transition committed inside the statement"
        )
        assert (
            s.execute(
                select(Workspace.lifecycle_generation).where(Workspace.id == ws_id)
            ).scalar()
            == 9
        ), "the transition thread did not move the workspace to generation 9"
