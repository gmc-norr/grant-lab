import asyncio
import uuid

import pytest

from call_manager.config import Settings
from call_manager.db.models import ReviewRun, ReviewRunItem
from call_manager.proposal_assist.drafting.contract import TurnDoneEvent
from call_manager.proposal_assist.review.jobs import enqueue_review_run
from call_manager.proposal_assist.review.pass_runner import run_review_pass


class _Handle:
    def __init__(self, ws_id):
        self.workspace_id = ws_id


def make_fake_builder(outer_session_factory, behavior):
    def builder(*, settings, session_factory, workspace_id, review_run_id, item_id, kind):
        class _Fake:
            async def launch(self, ctx):
                return _Handle(workspace_id)

            async def send_turn(self, handle, instruction):
                b = behavior.get(kind, "ok")
                if b == "timeout":
                    await asyncio.sleep(settings.review_persona_timeout_seconds + 5)
                if b == "fail":
                    raise RuntimeError("boom")
                with outer_session_factory() as s:
                    s.get(ReviewRunItem, item_id).raw_response_json = {"markdown": f"# {kind}\nok"}
                    s.commit()
                yield TurnDoneEvent(tokens_in=1000, tokens_out=500, model="claude-opus-4-7")

            async def terminate(self, handle):
                return None

        return _Fake()

    return builder


def _claim_running(session_factory, ws_id, draft_sha):
    with session_factory() as s:
        run = enqueue_review_run(s, workspace_id=ws_id, draft_sha=draft_sha)
        run.status = "running"
        s.commit()
        return run.id


@pytest.mark.asyncio
async def test_happy_path_all_done(review_active_workspace, _settings_env):
    session_factory, ws_id, wdir, repo, draft_sha = review_active_workspace
    run_id = _claim_running(session_factory, ws_id, draft_sha)
    settings = Settings()
    settings.workspaces_dir = str(wdir)
    await run_review_pass(
        run_id=run_id,
        session_factory=session_factory,
        settings=settings,
        adapter_builder=make_fake_builder(session_factory, {}),
    )
    with session_factory() as s:
        run = s.get(ReviewRun, run_id)
        assert run.status == "done"
        assert run.fit_check_sha and run.rubric_review_sha
        assert run.estimated_dollars and run.input_tokens == 6000
        items = s.query(ReviewRunItem).filter_by(review_run_id=run_id).all()
        assert len(items) == 6 and all(i.status == "done" for i in items)


@pytest.mark.asyncio
async def test_one_persona_fails_is_partial(review_active_workspace, _settings_env):
    session_factory, ws_id, wdir, repo, draft_sha = review_active_workspace
    run_id = _claim_running(session_factory, ws_id, draft_sha)
    settings = Settings()
    settings.workspaces_dir = str(wdir)
    await run_review_pass(
        run_id=run_id,
        session_factory=session_factory,
        settings=settings,
        adapter_builder=make_fake_builder(session_factory, {"persona_domain_skeptic": "fail"}),
    )
    with session_factory() as s:
        run = s.get(ReviewRun, run_id)
        assert run.status == "partial"
        assert run.rubric_review_sha  # consolidation still ran


@pytest.mark.asyncio
async def test_fit_check_fail_with_consolidation_is_partial(review_active_workspace, _settings_env):
    session_factory, ws_id, wdir, repo, draft_sha = review_active_workspace
    run_id = _claim_running(session_factory, ws_id, draft_sha)
    settings = Settings()
    settings.workspaces_dir = str(wdir)
    await run_review_pass(
        run_id=run_id,
        session_factory=session_factory,
        settings=settings,
        adapter_builder=make_fake_builder(session_factory, {"fit_check": "fail"}),
    )
    with session_factory() as s:
        run = s.get(ReviewRun, run_id)
        assert run.status == "partial"
        assert run.fit_check_sha is None and run.rubric_review_sha


@pytest.mark.asyncio
async def test_all_personas_fail_is_failed(review_active_workspace, _settings_env):
    session_factory, ws_id, wdir, repo, draft_sha = review_active_workspace
    run_id = _claim_running(session_factory, ws_id, draft_sha)
    behavior = {
        k: "fail"
        for k in (
            "persona_domain_skeptic",
            "persona_methodology_critic",
            "persona_translation_skeptic",
            "persona_devils_advocate",
        )
    }
    settings = Settings()
    settings.workspaces_dir = str(wdir)
    await run_review_pass(
        run_id=run_id,
        session_factory=session_factory,
        settings=settings,
        adapter_builder=make_fake_builder(session_factory, behavior),
    )
    with session_factory() as s:
        run = s.get(ReviewRun, run_id)
        assert run.status == "failed" and run.rubric_review_sha is None


@pytest.mark.asyncio
async def test_subcall_skips_when_run_no_longer_running(review_active_workspace, _settings_env):
    # Codex High: a queued-behind-the-semaphore sub-call must NOT launch (spend
    # tokens / send workspace content) once the run has been cancelled/reaped.
    from call_manager.proposal_assist.review import pass_runner as pr

    session_factory, ws_id, wdir, repo, draft_sha = review_active_workspace
    with session_factory() as s:
        run = enqueue_review_run(s, workspace_id=ws_id, draft_sha=draft_sha)
        run.status = "running"
        s.flush()
        s.add(ReviewRunItem(review_run_id=run.id, kind="fit_check", status="queued"))
        run_id = run.id
        s.commit()
    with session_factory() as s:  # cancel between the read session and the sub-call
        s.get(ReviewRun, run_id).status = "cancelled"
        s.commit()

    launched = {"v": False}

    def builder(*, settings, session_factory, workspace_id, review_run_id, item_id, kind):
        class _Fake:
            async def launch(self, ctx):
                launched["v"] = True
                return _Handle(workspace_id)

            async def send_turn(self, handle, instruction):
                yield TurnDoneEvent(tokens_in=1, tokens_out=1, model="m")

            async def terminate(self, handle):
                return None

        return _Fake()

    settings = Settings()
    settings.workspaces_dir = str(wdir)
    await pr._run_one_call(
        kind="fit_check",
        instruction="x",
        run_id=run_id,
        workspace_id=ws_id,
        session_factory=session_factory,
        settings=settings,
        adapter_builder=builder,
    )
    assert launched["v"] is False
    with session_factory() as s:
        item = s.query(ReviewRunItem).filter_by(review_run_id=run_id, kind="fit_check").one()
        assert item.status == "cancelled"


@pytest.mark.asyncio
async def test_artifact_commit_failure_fails_run(review_active_workspace, monkeypatch, _settings_env):
    # Codex Medium: a git failure in finalize must fail the run immediately, not
    # leave it 'running' until the reaper TTL.
    from call_manager.proposal_assist.citations.gitio import GitCommitError
    from call_manager.proposal_assist.review import pass_runner as pr

    session_factory, ws_id, wdir, repo, draft_sha = review_active_workspace
    run_id = _claim_running(session_factory, ws_id, draft_sha)

    def boom(*a, **k):
        raise GitCommitError("disk full")

    monkeypatch.setattr(pr, "write_and_commit_files", boom)
    settings = Settings()
    settings.workspaces_dir = str(wdir)
    await run_review_pass(
        run_id=run_id,
        session_factory=session_factory,
        settings=settings,
        adapter_builder=make_fake_builder(session_factory, {}),
    )
    with session_factory() as s:
        assert s.get(ReviewRun, run_id).status == "failed"
