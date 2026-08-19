"""F3b — the two ORDERED LOCK CASES that prove the shared-lease protocol.

Not a straddle. Pausing an effect mid-flight and committing a lifecycle
transition in the gap deadlocks a CORRECT implementation: the paused effect
holds the shared lease and the transition needs the exclusive lock. What is
provable is which side wins the lock, in both directions.
"""

from __future__ import annotations

import threading
import time
import uuid
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
        stamp = FenceStamp(ws_id, _G, None, job_id)

        def call():
            effects.submit_scorecard(stamp, SubmitScorecard(scorecard={"ok": 1}))

        def landed(s):
            return s.get(EvaluatorJob, job_id).scorecard is not None

    elif name == "submit_review":
        run_id, item_id = seed_review_item(session, ws_id, generation=_G)
        stamp = FenceStamp(ws_id, _G, None, run_id)

        def call():
            effects.submit_review(
                stamp, SubmitReview(review_run_item_id=item_id, markdown="body")
            )

        def landed(s):
            return s.get(ReviewRunItem, item_id).raw_response_json is not None

    elif name == "submit_uncited_claims":
        job_id = seed_uncited_job(session, ws_id, generation=_G)
        stamp = FenceStamp(ws_id, _G, None, job_id)

        def call():
            effects.submit_uncited_claims(stamp, SubmitUncitedClaims(claims=[]))

        def landed(s):
            return s.get(UncitedClaimsJob, job_id).claims is not None

    elif name == "propose_artifact_edit":
        stamp = FenceStamp(ws_id, _G, None, None)

        def call():
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

        def landed(s):
            return s.query(ProposedChange).filter_by(workspace_id=ws_id).count() == 1

    elif name == "propose_citation_insert":
        stamp = FenceStamp(ws_id, _G, None, None)

        def call():
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

        def landed(s):
            return s.query(ProposedChange).filter_by(workspace_id=ws_id).count() == 1

    else:  # pragma: no cover - guards a typo in EFFECT_NAMES
        raise AssertionError(f"unknown effect {name!r}")

    return ws_id, call, landed


@contextmanager
def _hold_the_lease_for(engine, seconds: float, signal: threading.Event):
    """Pause the effect's own thread immediately AFTER it wins the shared lease
    and while it still holds it, so the lifecycle transition provably queues
    behind it. Surgical: no production hook, and the pause is on the effect's
    connection inside its own transaction."""

    def _hook(conn, cursor, statement, params, context, executemany):
        if "pg_advisory_xact_lock_shared" in statement:
            signal.set()
            time.sleep(seconds)

    event.listen(engine, "after_cursor_execute", _hook)
    try:
        yield
    finally:
        event.remove(engine, "after_cursor_execute", _hook)


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

    def _effect():
        call()

    def _lifecycle():
        lease_held.wait(timeout=10)
        with pg_session_factory() as s:
            acquire_commit_mutex(s, ws_id, timeout_seconds=20)
            # Ordering fact, not a timing artifact: we hold the EXCLUSIVE lock,
            # so if the effect's write is visible here it committed first.
            observed["effect_visible_when_lifecycle_won"] = landed(s)
            bump_generation(s, ws_id, to=_NEXT)
            s.commit()

    with _hold_the_lease_for(migrated_engine, 1.0, lease_held):
        threads = [threading.Thread(target=_effect), threading.Thread(target=_lifecycle)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

    assert observed["effect_visible_when_lifecycle_won"] is True
    with pg_session_factory() as s:
        assert landed(s) is True
        assert s.get(Workspace, ws_id).lifecycle_generation == _NEXT


@pytest.mark.parametrize("name", EFFECT_NAMES)
def test_case_b_lifecycle_first_the_effect_lands_nothing(name, pg_session_factory):
    """CASE B — the one that matters. The transition wins the exclusive lock; the
    effect blocks, then evaluates its predicate against the NEW generation and
    produces no semantic effect at all."""
    effects = SqlEffects(pg_session_factory)
    with pg_session_factory() as s:
        ws_id, call, landed = _build_case(s, effects, name)

    holder_ready = threading.Event()
    effect_starting = threading.Event()

    def _lifecycle():
        with pg_session_factory() as s:
            acquire_commit_mutex(s, ws_id, timeout_seconds=20)
            bump_generation(s, ws_id, to=_NEXT)
            holder_ready.set()
            effect_starting.wait(timeout=10)
            time.sleep(0.5)  # let the effect queue on the lease
            s.commit()

    t = threading.Thread(target=_lifecycle)
    t.start()
    try:
        assert holder_ready.wait(timeout=10)
        effect_starting.set()
        started = time.monotonic()
        with pytest.raises(WorkspaceGenerationStale) as e:
            call()
        waited = time.monotonic() - started
    finally:
        t.join(timeout=60)

    assert waited >= 0.4, "the effect did not block on the lease"
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

    def _transition():
        time.sleep(0.4)  # inside the sleeping statement's lifetime
        with pg_session_factory() as s:
            bump_generation(s, ws_id, to=9)
            s.commit()

    t = threading.Thread(target=_transition)
    t.start()
    with pg_session_factory() as s:
        s.execute(predicate_only, {"pc": pc_id, "ws": ws_id, "gen": 7})
        s.commit()
    t.join(timeout=30)

    with pg_session_factory() as s:
        assert s.get(ProposedChange, pc_id) is not None, (
            "the race did not reproduce — check that the transition committed "
            "DURING the sleeping statement, not before or after it"
        )
        assert s.execute(
            select(Workspace.lifecycle_generation).where(Workspace.id == ws_id)
        ).scalar() == 9
