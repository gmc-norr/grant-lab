"""Review 2026-09-02 A10 F1: a Phase-0 worker claimed at generation G wrote M2/M3
into generation G+2 after delete → restore.

Only `_m1` took Primitive B; `_m2`/`_m3` checked liveness (`soft_deleted_at IS
NULL`) and `status == 'running'`. After a restore both are true again — and the
re-queued row is re-claimed by a NEW worker, so the stale worker's milestones
passed every check, wrote `section_extracts` / the rubric / the
`rubric_initialized` event into the restored workspace, and set the other
worker's claim to `waiting_for_rubric_review` (the review's probe A, RAN).

Spec mechanism 3: the milestones already hold the Workspace row FOR UPDATE, so
they assert `lifecycle_generation == claimed_generation` on that locked row. No
new lock. And the lock-free pre-check refuses BEFORE the LLM call, so a stale
worker costs no cloud spend.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from sqlalchemy import func, select

from grant_lab.db.models import (
    CallRubric,
    CallSectionExtract,
    Workspace,
    WorkspaceEvent,
    WorkspaceSetupJob,
)
from grant_lab.proposal_assist.phases.setup import (
    Phase0Worker,
    WorkspaceGoneDuringSetup,
    claim_next_setup_job,
)
from grant_lab.workspaces import restore_workspace_db, soft_delete_workspace_db
from tests.proposal_assist.conftest import make_user, make_workspace
from tests.proposal_assist.test_phase0_transaction_boundaries import _StubClient, _worker


@dataclass
class _CountingClient(_StubClient):
    calls: list = field(default_factory=list)

    def chat(self, *, messages, response_format=None):
        self.calls.append(messages)
        return super().chat(messages=messages, response_format=response_format)


def _stale_claim_after_restore(pg_session_factory, current_user, tmp_path, client):
    """Worker A claims at generation 0 and finishes _m1. The owner deletes, an
    admin restores (generation 2, job re-queued), and worker B claims the row
    (running, claimed_at_generation=2). Returns what A needs to resume."""
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        admin = make_user(s, email="restore-admin@umu.se")
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="queued"))
        s.commit()
        ws_id, admin_id = ws.id, admin.id
        job = claim_next_setup_job(session=s)  # worker A's claim
        assert job.claimed_at_generation == 0
        job_id = job.id
        s.commit()

    w = _worker(pg_session_factory, tmp_path, client)
    pinned = w._m1(job_id, ws_id, claimed_generation=0)

    with pg_session_factory() as s:
        soft_delete_workspace_db(s, workspace_id=ws_id)
        s.commit()
    with pg_session_factory() as s:
        outcome = restore_workspace_db(s, workspace_id=ws_id, admin_id=admin_id)
    assert outcome.generation == 2 and outcome.setup_job_restored_to == "queued"

    with pg_session_factory() as s:
        b_claim = claim_next_setup_job(session=s)  # worker B's claim
        assert b_claim is not None and b_claim.id == job_id
        assert b_claim.claimed_at_generation == 2
        s.commit()
    return w, job_id, ws_id, pinned


def _snapshot(pg_session_factory, ws_id, job_id):
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        job = s.get(WorkspaceSetupJob, job_id)
        return {
            "section_extracts": ws.section_extracts,
            "rubric_criteria": ws.rubric_criteria,
            "job_status": job.status,
            "job_generation": job.claimed_at_generation,
            "job_attempts": job.attempts,
            "extract_rows": s.scalar(
                select(func.count())
                .select_from(CallSectionExtract)
                .where(CallSectionExtract.call_id == ws.call_id)
            ),
            "rubric_rows": s.scalar(
                select(func.count()).select_from(CallRubric).where(CallRubric.call_id == ws.call_id)
            ),
            "rubric_events": s.scalar(
                select(func.count())
                .select_from(WorkspaceEvent)
                .where(
                    WorkspaceEvent.workspace_id == ws_id,
                    WorkspaceEvent.kind == "rubric_initialized",
                )
            ),
        }


def test_a_stale_worker_is_refused_before_it_spends_anything(
    pg_session_factory, current_user, tmp_path
):
    client = _CountingClient()
    w, job_id, ws_id, pinned = _stale_claim_after_restore(
        pg_session_factory, current_user, tmp_path, client
    )
    before = _snapshot(pg_session_factory, ws_id, job_id)
    assert before["job_status"] == "running" and before["job_generation"] == 2

    with pytest.raises(WorkspaceGoneDuringSetup):
        w._m2(job_id, ws_id, pinned, claimed_generation=0)
    with pytest.raises(WorkspaceGoneDuringSetup) as m3:
        w._m3(job_id, ws_id, pinned, claimed_generation=0)
    assert client.calls == [], "the pre-check must refuse BEFORE the LLM call"
    assert _snapshot(pg_session_factory, ws_id, job_id) == before

    # A's failure bookkeeping must not touch B's claim (Slice C1 rule: the
    # restore already answered this exception).
    w._fail(job_id, m3.value)
    after = _snapshot(pg_session_factory, ws_id, job_id)
    assert after == before


def test_m2_refuses_on_the_locked_row_even_if_the_precheck_is_bypassed(
    pg_session_factory, current_user, tmp_path, monkeypatch
):
    """The locked persist is its own gate: with `_assert_live` neutered the LLM
    call happens, but nothing is written into the restored workspace."""
    client = _CountingClient()
    w, job_id, ws_id, pinned = _stale_claim_after_restore(
        pg_session_factory, current_user, tmp_path, client
    )
    monkeypatch.setattr(Phase0Worker, "_assert_live", lambda self, *a, **k: None)
    before = _snapshot(pg_session_factory, ws_id, job_id)

    with pytest.raises(WorkspaceGoneDuringSetup):
        w._m2(job_id, ws_id, pinned, claimed_generation=0)
    assert len(client.calls) == 1  # it computed — and then refused to persist
    assert _snapshot(pg_session_factory, ws_id, job_id) == before


def test_m3_refuses_on_the_locked_row_even_if_the_precheck_is_bypassed(
    pg_session_factory, current_user, tmp_path, monkeypatch
):
    client = _CountingClient()
    w, job_id, ws_id, pinned = _stale_claim_after_restore(
        pg_session_factory, current_user, tmp_path, client
    )
    monkeypatch.setattr(Phase0Worker, "_assert_live", lambda self, *a, **k: None)
    before = _snapshot(pg_session_factory, ws_id, job_id)

    with pytest.raises(WorkspaceGoneDuringSetup):
        w._m3(job_id, ws_id, pinned, claimed_generation=0)
    after = _snapshot(pg_session_factory, ws_id, job_id)
    assert after == before
    assert after["job_status"] == "running", "B's claim must not be terminalized by A"
    assert after["rubric_events"] == 0


def test_a_current_worker_still_proceeds(pg_session_factory, current_user, tmp_path):
    """The gate compares, it does not merely refuse: worker B, whose claim IS
    generation 2, runs _m2 and _m3 to the yield."""
    client = _CountingClient()
    w, job_id, ws_id, pinned = _stale_claim_after_restore(
        pg_session_factory, current_user, tmp_path, client
    )
    w._m2(job_id, ws_id, pinned, claimed_generation=2)
    w._m3(job_id, ws_id, pinned, claimed_generation=2)
    after = _snapshot(pg_session_factory, ws_id, job_id)
    assert after["job_status"] == "waiting_for_rubric_review"
    assert after["rubric_events"] == 1
