"""Review 2026-09-02 A10 F2: the delete path and the turn-success finalize took
the workspace row and the turn_jobs row in opposite orders — a real Postgres
40P01 (RAN, probe B), and the finalize was the victim: its token stamp, the
`spend_ledger` row and the `cloud_turn_completed` event rolled back, and the
delete then cancelled the turn with `usage_unknown=True`.

Delete is M → W → J (the `UPDATE workspaces` autoflushes before the job-table
UPDATEs). Every other writer is W → J. `_finalize_success` was J → W. Now it
takes the workspace row FOR NO KEY UPDATE first — the same lock an UPDATE takes,
so it queues behind an in-flight delete instead of crossing it.

The gate pins the ORDER of statements inside the finalize's transaction, which is
the property; a two-thread deadlock reproduction is timing-dependent and proves
less.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import event

from grant_lab.proposal_assist.drafting.contract import AssistantTextEvent, TurnDoneEvent
from grant_lab.proposal_assist.turns.processor import process_turn_once
from tests.proposal_assist.test_turns_processor import _build_controller_and_submit

_WS_LOCK = re.compile(r"SELECT workspaces\.id\s+FROM workspaces\s+WHERE .*FOR NO KEY UPDATE", re.S)
_JOB_COMPLETE = re.compile(r"UPDATE turn_jobs SET", re.S)


@pytest.mark.asyncio
async def test_finalize_locks_the_workspace_row_before_the_turn_jobs_update(
    pg_session_factory, migrated_engine, _settings_env
):
    wsid, job_id, controller, _ = _build_controller_and_submit(
        pg_session_factory,
        _settings_env,
        [
            AssistantTextEvent(text="ok"),
            TurnDoneEvent(tokens_in=10, tokens_out=5, model="fake", session_id="s1"),
        ],
    )
    seen: list[tuple[str, object]] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        seen.append((statement, parameters))

    event.listen(migrated_engine, "before_cursor_execute", _record)
    try:
        await process_turn_once(
            session_factory=pg_session_factory, controller=controller, turn_job_id=job_id
        )
    finally:
        event.remove(migrated_engine, "before_cursor_execute", _record)

    completes = [
        i
        for i, (stmt, params) in enumerate(seen)
        if _JOB_COMPLETE.search(stmt) and "completed" in str(params)
    ]
    assert completes, "the finalize never ran its running→completed UPDATE"
    i_job = completes[0]
    ws_locks = [i for i, (stmt, _) in enumerate(seen) if _WS_LOCK.search(stmt)]
    assert ws_locks, "the finalize must take the workspace row (FOR NO KEY UPDATE) first"
    # The lock closest before the job UPDATE belongs to the finalize's own
    # transaction; nothing may sit between them that commits.
    i_ws = max(i for i in ws_locks if i < i_job) if any(i < i_job for i in ws_locks) else None
    assert i_ws is not None, "the workspace row lock came AFTER the turn_jobs UPDATE (J → W)"
    between = [stmt for stmt, _ in seen[i_ws:i_job]]
    assert not any(s.strip().upper().startswith("COMMIT") for s in between)
