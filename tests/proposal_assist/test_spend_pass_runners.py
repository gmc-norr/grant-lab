"""Every terminal path of every pass runner leaves a row.

Verified defects this pins:
  * evaluator returns on "no scorecard" ABOVE its token write, so a run that
    spent tokens and produced nothing recorded zero;
  * every _fail helper writes no token fields at all;
  * all of them write tokens only `if done is not None`, and `done` was bound
    only when the inner coroutine RETURNED, so an exception after the done event
    threw the event away (Step 3b);
  * `status` has three legal values and no recipe could produce the third.

All of them lose spend in the same direction: money out, ledger silent.

FAMILIES excludes review deliberately. Review is a six-call fan-out that records
one row per ReviewRunItem, so the `.scalar_one()` shape below cannot hold for it;
its tests are the second half of this file.
"""

import asyncio
import subprocess

import pytest
from sqlalchemy import select

from call_manager.config import Settings
from call_manager.db.models import (
    CoherenceJob,
    EvaluatorJob,
    LlmSpend,
    ReviewRunItem,
    UncitedClaimsJob,
)
from call_manager.proposal_assist.coherence.pass_runner import run_coherence_pass
from call_manager.proposal_assist.drafting.contract import TurnDoneEvent
from call_manager.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from call_manager.proposal_assist.evaluator.pass_runner import run_evaluator_pass
from call_manager.proposal_assist.review.jobs import enqueue_review_run
from call_manager.proposal_assist.review.pass_runner import run_review_pass
from call_manager.proposal_assist.review.personas import WAVE1_KINDS
from call_manager.proposal_assist.spend import record_spend, sum_spend_today
from call_manager.proposal_assist.uncited_claims.pass_runner import run_uncited_claims_pass
from tests.proposal_assist.conftest import make_user, make_workspace, review_stamp

FAMILIES = ["coherence", "evaluator", "uncited_claims"]

JOB_MODEL = {
    "coherence": CoherenceJob,
    "evaluator": EvaluatorJob,
    "uncited_claims": UncitedClaimsJob,
}

_ENTRY = {
    "coherence": run_coherence_pass,
    "evaluator": run_evaluator_pass,
    "uncited_claims": run_uncited_claims_pass,
}


def _seed_repo(workspaces_dir, ws_id):
    repo = workspaces_dir / str(ws_id) / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    # section 1 holds the verbatim uncited-claims text; the file doubles as the
    # coherence/evaluator draft.
    (repo / "proposal.md").write_text("## Aims\n\nWe claim 30% relapse occurs.\n")
    (repo / "proposal-spec.md").write_text("## Aims\n\nSpec.\n")


def _make_job(s, family, ws_id):
    job = JOB_MODEL[family](workspace_id=ws_id, status="running")
    s.add(job)
    s.flush()
    return job


def _write_result(session_factory, family, job_id):
    """Simulate the submit_* tool writing this family's required result onto the
    job, so `outcome="ok"` / `"no_done"` reaches the SUCCESS finalize."""
    if family == "evaluator":
        with session_factory() as s:
            s.get(EvaluatorJob, job_id).scorecard = {
                "criteria": [],
                "overall_band": "gap",
                "summary": "x",
            }
            s.commit()
    elif family == "uncited_claims":
        with session_factory() as s:
            s.get(UncitedClaimsJob, job_id).claims = [
                {"claim_text": "30% relapse", "section_index": 1, "reason": "stat"},
            ]
            s.commit()
    # coherence needs no result row to complete.


def _stub_adapter(session_factory, family, job_id, *, outcome, tokens_in, tokens_out):
    """A FakeAgentAdapter whose send_turn drives the requested terminal path.

    | outcome        | done event | result written | then                    |
    |----------------|------------|----------------|-------------------------|
    | ok             | yes        | yes            | -> success finalize     |
    | failed         | yes        | (n/a)          | raise RuntimeError       |
    | timeout        | yes        | (n/a)          | raise asyncio.TimeoutError|
    | no_done        | NO         | yes            | -> success finalize      |
    | no_scorecard   | yes        | NO             | -> evaluator _fail       |
    """
    done_evt = TurnDoneEvent(
        tokens_in=tokens_in, tokens_out=tokens_out, model="claude-x", session_id="sess-1"
    )

    class _Stub(FakeAgentAdapter):
        async def send_turn(self, handle, user_message):
            if outcome in ("ok", "no_done"):
                _write_result(session_factory, family, job_id)
            if outcome == "no_done":
                # a valid result, but the stream reports no usage counts
                return
                yield  # pragma: no cover  (keep this an async generator)
            # every other outcome emits the done event first, so the runner has
            # a usage figure to record even when the call then blows up.
            yield done_evt
            if outcome == "failed":
                raise RuntimeError("sdk exploded")
            if outcome == "timeout":
                raise TimeoutError

    return _Stub()


@pytest.fixture
def run_pass(tmp_path, pg_session_factory, _settings_env):
    def _run(family, *, outcome, tokens_in=0, tokens_out=0):
        settings = Settings()
        settings.workspaces_dir = str(tmp_path)
        with pg_session_factory() as s:
            ws = make_workspace(s)
            s.flush()
            job = _make_job(s, family, ws.id)
            s.commit()
            ws_id, job_id = ws.id, job.id
        _seed_repo(tmp_path, ws_id)

        stub = _stub_adapter(
            pg_session_factory,
            family,
            job_id,
            outcome=outcome,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
        asyncio.run(
            _ENTRY[family](
                job_id=job_id,
                claimed_generation=0,
                session_factory=pg_session_factory,
                settings=settings,
                adapter_builder=lambda **kw: stub,
            )
        )
        return ws_id, job_id

    return _run


@pytest.mark.parametrize("family", FAMILIES)
def test_a_QUEUED_job_refuses_when_the_global_cap_is_reached(
    pg_session_factory, run_pass, monkeypatch, family
):
    """The endpoint checked the global cap at ENQUEUE time. That answer is stale by
    the time the drain tick runs.

    Without a recheck here, a job accepted before the org hit its ceiling still
    spends past it — the "contributes to the total, never consults it" shape this
    whole branch exists to remove, surviving inside the branch that removes it.
    Seeded over the ceiling BEFORE the pass runs, so only the runner's own recheck
    can stop it.
    """
    monkeypatch.setenv("GLOBAL_DAILY_TOKEN_CAP_INPUT", "100")
    with pg_session_factory() as s:
        # A distinct owner: run_pass creates its own workspace with the default
        # owner email, and users.email is unique.
        ws = make_workspace(s, owner=make_user(s, email=f"capseed-{family}@umu.se"))
        s.flush()
        record_spend(
            s,
            workspace_id=ws.id,
            kind="chat_turn",
            status="ok",
            tokens_in=5000,
            tokens_out=0,
        )
        s.commit()

    ws_id, job_id = run_pass(family, outcome="ok", tokens_in=21, tokens_out=34)

    with pg_session_factory() as s:
        job = s.get(JOB_MODEL[family], job_id)
        assert job.status == "failed"
        assert "global" in (job.error or "").lower()
        # The hosted call never happened, so no row for THIS job's own spend.
        rows = (
            s.execute(
                select(LlmSpend).where(
                    LlmSpend.kind == family, LlmSpend.ref_id == str(job_id)
                )
            )
            .scalars()
            .all()
        )
        assert [r.tokens_in for r in rows] == [0], rows


@pytest.mark.parametrize("family", FAMILIES)
def test_a_successful_run_records(pg_session_factory, run_pass, family):
    ws_id, job_id = run_pass(family, outcome="ok", tokens_in=21, tokens_out=34)
    with pg_session_factory() as s:
        row = s.execute(select(LlmSpend).where(LlmSpend.kind == family)).scalar_one()
        assert (row.tokens_in, row.tokens_out, row.status) == (21, 34, "ok")
        assert row.ref_id == str(job_id)


@pytest.mark.parametrize("family", FAMILIES)
def test_a_FAILED_run_records(pg_session_factory, run_pass, family):
    ws_id, job_id = run_pass(family, outcome="failed", tokens_in=21, tokens_out=0)
    with pg_session_factory() as s:
        row = s.execute(select(LlmSpend).where(LlmSpend.kind == family)).scalar_one()
        assert row.status == "failed"
        assert row.tokens_in == 21


@pytest.mark.parametrize("family", FAMILIES)
def test_a_TIMED_OUT_run_records_timeout(pg_session_factory, run_pass, family):
    # `status` is CHECK-constrained to ok|failed|timeout, and before this test no
    # line in the plan could ever produce the third — while the source computes
    # the distinction one branch earlier (`isinstance(exc, (asyncio.TimeoutError,
    # TimeoutError))` at coherence:118-121, evaluator:144-148,
    # uncited_claims:159-163) and then threw it away. Equality, not a
    # `status in ("failed", "timeout")` disjunction that passes on "failed".
    ws_id, job_id = run_pass(family, outcome="timeout", tokens_in=21, tokens_out=0)
    with pg_session_factory() as s:
        row = s.execute(select(LlmSpend).where(LlmSpend.kind == family)).scalar_one()
        assert row.status == "timeout"
        assert row.tokens_in == 21


@pytest.mark.parametrize("family", FAMILIES)
def test_a_run_with_NO_done_event_still_records(pg_session_factory, run_pass, family):
    # `done is None` was the guard that skipped the write entirely. The pass
    # itself SUCCEEDS here (see the fixture spec) — all three families reach
    # their success finalize and complete — so the row is an honest "we called
    # the model and it reported no counts", which is a different fact from "we
    # spent nothing" and a different fact again from no row at all.
    ws_id, job_id = run_pass(family, outcome="no_done")
    with pg_session_factory() as s:
        row = s.execute(select(LlmSpend).where(LlmSpend.kind == family)).scalar_one()
        assert (row.tokens_in, row.tokens_out) == (0, 0)
        assert row.status == "ok"


def test_evaluator_records_when_it_produced_no_scorecard(pg_session_factory, run_pass):
    # The exact shape of the cited defect: pass_runner.py's `if job.scorecard is
    # None: _fail(...); return` (:177-180) sat ABOVE the token write (:183-187).
    ws_id, job_id = run_pass("evaluator", outcome="no_scorecard", tokens_in=99, tokens_out=5)
    with pg_session_factory() as s:
        row = s.execute(select(LlmSpend).where(LlmSpend.kind == "evaluator")).scalar_one()
        assert row.tokens_in == 99, "tokens were spent producing nothing, and must be recorded"
        assert row.status == "failed"


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("outcome", ["ok", "failed"])
def test_the_display_columns_still_agree_with_the_ledger(
    pg_session_factory, run_pass, family, outcome
):
    # The per-job columns stay as a cache for the UI. A cache that disagrees with
    # the source of truth is worse than no cache — Task 9 gates this statically,
    # this checks it at runtime. It must LOAD the job row: the version this
    # replaced only re-read the ledger and could not go red on its own.
    #
    # The `failed` case is the one that bites. The only column writers in all
    # three families sit on the success path (coherence:171-172,
    # evaluator:184-185, uncited_claims:191-192), and those columns are NOT NULL
    # with server_default "0" (models.py:988-989, :1044-1045, :1096-1097), so a
    # _fail that records spend and writes no column reads 21 in the ledger and 0
    # in the cache. Step 5 adds the column write that makes this pass.
    ws_id, job_id = run_pass(family, outcome=outcome, tokens_in=8, tokens_out=9)
    with pg_session_factory() as s:
        row = s.execute(select(LlmSpend).where(LlmSpend.kind == family)).scalar_one()
        job = s.get(JOB_MODEL[family], job_id)
        assert (job.input_tokens, job.output_tokens) == (row.tokens_in, row.tokens_out)


# --- review: one row per sub-call, not one per run -------------------------


def _review_repo(workspaces_dir, ws_id):
    repo = workspaces_dir / str(ws_id) / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    proposal_bytes = b"# Draft\n\nThis is the draft proposal text.\n"
    (repo / "proposal.md").write_bytes(proposal_bytes)
    (repo / "proposal-spec.md").write_text("## Aims\n\nSpec text.\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True)
    import hashlib

    return hashlib.sha256(proposal_bytes).hexdigest()


def _review_builder(outer_session_factory, *, outcome, tokens_in, tokens_out):
    """Every sub-call gets the requested outcome. On ok, it writes the item's
    markdown then emits a done event; on failed/timeout it emits a done event
    (so `done` is bound) and then raises, exercising the hoisted `done`."""

    class _Handle:
        def __init__(self, ws_id):
            self.workspace_id = ws_id

    def builder(*, settings, session_factory, workspace_id, review_run_id, item_id, kind,
                lifecycle_generation):
        class _Fake:
            async def launch(self, ctx):
                return _Handle(workspace_id)

            async def send_turn(self, handle, instruction):
                if outcome == "ok":
                    with outer_session_factory() as s:
                        s.get(ReviewRunItem, item_id).raw_response_json = {
                            "markdown": f"# {kind}\nok"
                        }
                        s.commit()
                    yield TurnDoneEvent(
                        tokens_in=tokens_in, tokens_out=tokens_out, model="claude-x"
                    )
                    return
                # failed / timeout: emit the usage first, THEN blow up, so the
                # recorded row must come from the hoisted `done`.
                yield TurnDoneEvent(tokens_in=tokens_in, tokens_out=tokens_out, model="claude-x")
                if outcome == "failed":
                    raise RuntimeError("boom")
                if outcome == "timeout":
                    raise TimeoutError

            def unconfirmed_launch_ids(self):
                return []

            async def retire_unconfirmed(self) -> int:
                return 0

            async def terminate(self, handle):
                return None

        return _Fake()

    return builder


@pytest.fixture
def run_review(tmp_path, pg_session_factory, _settings_env):
    def _run(*, outcome, tokens_in=0, tokens_out=0):
        settings = Settings()
        settings.workspaces_dir = str(tmp_path)
        with pg_session_factory() as s:
            ws = make_workspace(s, current_phase="active")
            ws.rubric_criteria = [
                {"name": "Scientific quality", "description": "Rigour", "weight": 0.6},
                {"name": "Impact", "description": "Reach", "weight": 0.4},
            ]
            ws.rubric_funder_notes = "Swedish university-hospital context."
            s.flush()
            ws_id = ws.id
            s.commit()
        draft_sha = _review_repo(tmp_path, ws_id)
        with pg_session_factory() as s:
            run = enqueue_review_run(s, workspace_id=ws_id, draft_sha=draft_sha)
            run.status = "running"
            s.commit()
            run_id = run.id

        asyncio.run(
            run_review_pass(
                run_id=run_id,
                stamp=review_stamp(pg_session_factory, run_id, 0),
                session_factory=pg_session_factory,
                settings=settings,
                adapter_builder=_review_builder(
                    pg_session_factory,
                    outcome=outcome,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                ),
            )
        )

        # Measure which items actually ran a sub-call: a sub-call sets started_at.
        # ok -> 6 (5 wave-1 + consolidation); failed/timeout -> 5 (consolidation
        # skipped because every persona failed).
        with pg_session_factory() as s:
            items = s.scalars(
                select(ReviewRunItem).where(ReviewRunItem.review_run_id == run_id)
            ).all()
            ran = [i for i in items if i.started_at is not None]
            item_ids = [i.id for i in ran]
        return len(ran), item_ids, ws_id, run_id

    return _run


def test_review_records_one_row_per_sub_call(pg_session_factory, run_review):
    # Up to SIX hosted calls per run, each with its own adapter and its own
    # terminal status. A single run-level row would put all six behind one
    # status and one ref_id.
    n_calls, item_ids, ws_id, run_id = run_review(outcome="ok", tokens_in=21, tokens_out=0)
    with pg_session_factory() as s:
        rows = s.execute(select(LlmSpend).where(LlmSpend.kind == "review")).scalars().all()
        assert len(rows) == n_calls
        assert {r.ref_id for r in rows} == {str(i) for i in item_ids}
        assert sum(r.tokens_in for r in rows) == 21 * n_calls
        assert {r.status for r in rows} == {"ok"}


def test_a_FAILED_review_sub_call_records(pg_session_factory, run_review):
    n_calls, item_ids, ws_id, run_id = run_review(outcome="failed", tokens_in=21, tokens_out=0)
    with pg_session_factory() as s:
        rows = s.execute(select(LlmSpend).where(LlmSpend.kind == "review")).scalars().all()
        assert len(rows) == n_calls
        assert {r.status for r in rows} == {"failed"}
        assert all(r.tokens_in == 21 for r in rows)


def test_a_TIMED_OUT_review_sub_call_records_timeout(pg_session_factory, run_review):
    n_calls, item_ids, ws_id, run_id = run_review(outcome="timeout", tokens_in=21, tokens_out=0)
    with pg_session_factory() as s:
        rows = s.execute(select(LlmSpend).where(LlmSpend.kind == "review")).scalars().all()
        assert len(rows) == n_calls
        assert {r.status for r in rows} == {"timeout"}


def test_the_review_item_columns_still_agree_with_the_ledger(pg_session_factory, run_review):
    n_calls, item_ids, ws_id, run_id = run_review(outcome="ok", tokens_in=8, tokens_out=9)
    with pg_session_factory() as s:
        rows = s.execute(select(LlmSpend).where(LlmSpend.kind == "review")).scalars().all()
        by_ref = {r.ref_id: r for r in rows}
        for item_id in item_ids:
            item = s.get(ReviewRunItem, item_id)
            row = by_ref[str(item_id)]
            assert (item.input_tokens, item.output_tokens) == (row.tokens_in, row.tokens_out)


def test_the_run_rollup_writes_no_ledger_row(pg_session_factory, run_review):
    # review/jobs.py:rollup_run_totals (:142-155) re-derives the run totals from
    # the items and has THREE call sites: pass_runner.py:174, pass_runner.py:364
    # and jobs.py:184. A reaped-then-late-finalized run reaches it twice, so an
    # append-only row written there would double-count against the very cap this
    # ledger exists to make correct.
    n_calls, item_ids, ws_id, run_id = run_review(outcome="ok", tokens_in=8, tokens_out=9)
    with pg_session_factory() as s:
        rows = s.execute(select(LlmSpend).where(LlmSpend.kind == "review")).scalars().all()
        # Count first. `all(...)` over an empty list is True, so without this the
        # assertion below would pass before any code is written — the exact
        # hollow-gate shape this plan keeps catching.
        assert len(rows) == n_calls
        assert all(r.ref_id != str(run_id) for r in rows), "no run-level review row"


def test_review_CONSOLIDATION_refuses_after_wave_one_exhausts_the_global_cap(
    tmp_path, pg_session_factory, _settings_env, monkeypatch
):
    """The pass-start recheck runs ONCE, before the five wave-1 calls.

    Consolidation launches after those five have committed their spend, so a run
    that was legitimately under the ceiling at pass start could still make a
    sixth hosted call after crossing it. Only the per-sub-call check inside
    `_drive` can stop that.

    The arithmetic is pinned so the denial lands on consolidation and nowhere
    else. Writing T for whatever today's ledger already holds, the ceiling is set
    at T+100: concurrency 1 makes wave-1 strictly sequential, and its five calls
    at 21 input tokens each check at T, T+21 ... T+84 — all under — leaving the
    total at T+105. Consolidation is the first call to see a total at or over
    the cap (`check_global_daily_token_cap` denies on `used_in >= cap_in`).

    A first draft seeded 90 and let wave-1 deny ITSELF: every persona failed,
    consolidation was skipped as "all personas failed", and the test passed
    without the cap check existing. Hence the exact-set assertion below — the
    five wave-1 calls must all have reached the model.
    """
    # The ceiling is set RELATIVE to what today's ledger already holds. A fixed
    # "100" passed this file alone and failed the full run: xdist gives each
    # worker one Postgres and `--dist loadfile` puts many files on it, so every
    # other test that records spend lands in the same Stockholm day and the cap
    # was already blown before pass start. Reading the total here makes the
    # arithmetic below hold wherever this test runs in the order.
    with pg_session_factory() as s:
        used_in, _ = sum_spend_today(s)
    monkeypatch.setenv("GLOBAL_DAILY_TOKEN_CAP_INPUT", str(used_in + 100))
    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    settings.review_persona_concurrency = 1

    with pg_session_factory() as s:
        ws = make_workspace(s, current_phase="active")
        ws.rubric_criteria = [
            {"name": "Scientific quality", "description": "Rigour", "weight": 1.0},
        ]
        s.flush()
        ws_id = ws.id
        s.commit()

    draft_sha = _review_repo(tmp_path, ws_id)
    with pg_session_factory() as s:
        run = enqueue_review_run(s, workspace_id=ws_id, draft_sha=draft_sha)
        run.status = "running"
        s.commit()
        run_id = run.id

    reached: list[str] = []
    inner = _review_builder(pg_session_factory, outcome="ok", tokens_in=21, tokens_out=0)

    def counting_builder(**kwargs):
        adapter = inner(**kwargs)
        kind = kwargs["kind"]
        real_send = adapter.send_turn

        async def send_turn(handle, instruction):
            reached.append(kind)
            async for event in real_send(handle, instruction):
                yield event

        adapter.send_turn = send_turn
        return adapter

    asyncio.run(
        run_review_pass(
            run_id=run_id,
            stamp=review_stamp(pg_session_factory, run_id, 0),
            session_factory=pg_session_factory,
            settings=settings,
            adapter_builder=counting_builder,
        )
    )

    # All five wave-1 calls ran; consolidation is the ONLY one denied. Without
    # this exact set, "consolidation never ran" is also true when wave-1 failed
    # and consolidation was skipped for having nothing to consolidate.
    assert sorted(reached) == sorted(WAVE1_KINDS), reached

    with pg_session_factory() as s:
        cons = s.scalar(
            select(ReviewRunItem).where(
                ReviewRunItem.review_run_id == run_id,
                ReviewRunItem.kind == "consolidation",
            )
        )
        assert cons.status == "failed", (cons.status, cons.error_message)
        # Named, not just "failed": any sub-call exception also lands as failed,
        # so a bare status assert would pass on an unrelated break.
        assert cons.error_message == "global daily token cap reached"
        row = s.scalar(
            select(LlmSpend).where(
                LlmSpend.kind == "review", LlmSpend.ref_id == str(cons.id)
            )
        )
        # Denied is still a terminal path, so it still writes a row — a zero one.
        assert row is not None, "denied consolidation wrote no ledger row"
        assert (row.tokens_in, row.tokens_out) == (0, 0), row
