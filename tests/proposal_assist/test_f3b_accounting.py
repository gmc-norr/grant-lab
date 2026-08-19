"""F3b — accounting: owner-bound, atomic, and deliberately UNFENCED."""

from __future__ import annotations

import uuid

from call_manager.db.models import TurnJob
from call_manager.proposal_assist.port_impl.accounting import SqlAccounting
from call_manager.proposal_assist.ports.accounting import (
    MAX_FETCHES_PER_TURN,
    Accounting,
)
from tests.proposal_assist.f3b_seeds import (
    bump_generation,
    seed_running_turn,
    seed_workspace,
)


def test_the_implementation_satisfies_the_protocol(pg_session_factory):
    assert isinstance(SqlAccounting(pg_session_factory), Accounting)


def test_reserve_increments_and_returns_the_new_count(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=1)
        job_id = seed_running_turn(s, ws_id)

    acct = SqlAccounting(pg_session_factory)
    assert acct.reserve_fetch_slot(job_id) == 1
    assert acct.reserve_fetch_slot(job_id) == 2


def test_reserve_refuses_past_the_cap(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=1)
        job_id = seed_running_turn(s, ws_id)

    acct = SqlAccounting(pg_session_factory)
    for expected in range(1, MAX_FETCHES_PER_TURN + 1):
        assert acct.reserve_fetch_slot(job_id) == expected
    assert acct.reserve_fetch_slot(job_id) is None

    with pg_session_factory() as s:
        assert s.get(TurnJob, job_id).fetch_count == MAX_FETCHES_PER_TURN


def test_refund_never_pushes_the_count_negative(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=1)
        job_id = seed_running_turn(s, ws_id)

    acct = SqlAccounting(pg_session_factory)
    acct.refund_fetch_slot(job_id)
    with pg_session_factory() as s:
        assert s.get(TurnJob, job_id).fetch_count == 0


def test_accounting_still_lands_on_a_stale_generation(pg_session_factory):
    """DELIBERATE. A stale actor must still release what it reserved; fencing
    this would strand the job holding its slot."""
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=1)
        job_id = seed_running_turn(s, ws_id)

    acct = SqlAccounting(pg_session_factory)
    acct.reserve_fetch_slot(job_id)

    with pg_session_factory() as s:
        bump_generation(s, ws_id, to=4)
        s.commit()

    acct.refund_fetch_slot(job_id)
    with pg_session_factory() as s:
        assert s.get(TurnJob, job_id).fetch_count == 0


def test_a_stale_actors_refund_cannot_touch_the_new_turns_slot(pg_session_factory):
    """The interleaving the ownership binding exists for.

    Reserve at generation G, delete and restore, start a NEW turn at G+1. The old
    actor's refund must not decrement the new turn's count. An implementation
    targeting `workspace_id AND status='running'` — what references.py does today
    — takes a slot straight out of the new turn.
    """
    with pg_session_factory() as s:
        ws_id = seed_workspace(s, generation=1)
        old_turn = seed_running_turn(s, ws_id)

    acct = SqlAccounting(pg_session_factory)
    assert acct.reserve_fetch_slot(old_turn) == 1

    with pg_session_factory() as s:
        # delete + restore: the running turn is terminalized, the generation
        # advances, and a new turn starts. Only one turn per workspace may be
        # 'running' (unique partial index), so the old one MUST be terminal.
        s.get(TurnJob, old_turn).status = "failed"
        bump_generation(s, ws_id, to=2)
        s.commit()
        new_turn = seed_running_turn(s, ws_id)
        s.get(TurnJob, new_turn).fetch_count = 2
        s.commit()

    acct.refund_fetch_slot(old_turn)
    acct.reserve_fetch_slot(old_turn)

    with pg_session_factory() as s:
        assert s.get(TurnJob, new_turn).fetch_count == 2, (
            "the stale actor's accounting reached the NEW turn's row"
        )
        assert s.get(TurnJob, old_turn).fetch_count == 1


def test_an_unknown_turn_job_is_a_no_op(pg_session_factory):
    acct = SqlAccounting(pg_session_factory)
    assert acct.reserve_fetch_slot(uuid.uuid4()) is None
    acct.refund_fetch_slot(uuid.uuid4())  # must not raise
