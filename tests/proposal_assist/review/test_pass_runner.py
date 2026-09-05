import asyncio
import subprocess
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from grant_lab.config import Settings
from grant_lab.db.models import ReviewRun, ReviewRunItem
from grant_lab.proposal_assist.drafting.contract import TurnDoneEvent
from grant_lab.proposal_assist.review.jobs import (
    enqueue_review_run,
    reap_stuck_review_runs,
)
from grant_lab.proposal_assist.review.pass_runner import _run_one_call, run_review_pass
from tests.proposal_assist.conftest import review_stamp


class _Handle:
    def __init__(self, ws_id):
        self.workspace_id = ws_id



def repo_head(repo) -> str:
    """The repo's current HEAD sha, so a test can assert nothing was committed."""
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def make_fake_builder(outer_session_factory, behavior):
    def builder(*, settings, session_factory, workspace_id, review_run_id, item_id, kind,
                lifecycle_generation):
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

            def unconfirmed_launch_ids(self):
                return []

            async def retire_unconfirmed(self) -> int:
                return 0

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
        stamp=review_stamp(session_factory, run_id, 0),
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
        stamp=review_stamp(session_factory, run_id, 0),
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
        stamp=review_stamp(session_factory, run_id, 0),
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
        stamp=review_stamp(session_factory, run_id, 0),
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
    from grant_lab.proposal_assist.review import pass_runner as pr

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

    def builder(*, settings, session_factory, workspace_id, review_run_id, item_id, kind,
                lifecycle_generation):
        class _Fake:
            async def launch(self, ctx):
                launched["v"] = True
                return _Handle(workspace_id)

            async def send_turn(self, handle, instruction):
                yield TurnDoneEvent(tokens_in=1, tokens_out=1, model="m")

            def unconfirmed_launch_ids(self):
                return []

            async def retire_unconfirmed(self) -> int:
                return 0

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
        lifecycle_generation=0,
    )
    assert launched["v"] is False
    with session_factory() as s:
        item = s.query(ReviewRunItem).filter_by(review_run_id=run_id, kind="fit_check").one()
        assert item.status == "cancelled"


@pytest.mark.asyncio
async def test_artifact_commit_failure_fails_run(
    review_active_workspace, monkeypatch, _settings_env
):
    # Codex Medium: a git failure in finalize must fail the run immediately, not
    # leave it 'running' until the reaper TTL.
    from grant_lab.proposal_assist.citations.gitio import GitCommitError
    from grant_lab.proposal_assist.review import pass_runner as pr

    session_factory, ws_id, wdir, repo, draft_sha = review_active_workspace
    run_id = _claim_running(session_factory, ws_id, draft_sha)

    def boom(*a, **k):
        raise GitCommitError("disk full")

    monkeypatch.setattr(pr, "write_and_commit_files", boom)
    settings = Settings()
    settings.workspaces_dir = str(wdir)
    await run_review_pass(
        run_id=run_id,
        stamp=review_stamp(session_factory, run_id, 0),
        session_factory=session_factory,
        settings=settings,
        adapter_builder=make_fake_builder(session_factory, {}),
    )
    with session_factory() as s:
        assert s.get(ReviewRun, run_id).status == "failed"


@pytest.mark.asyncio
async def test_reaper_during_subcall_keeps_spend_and_does_not_resurrect(
    review_active_workspace, _settings_env
):
    """Codex High: a sub-call that finishes AFTER the reaper failed the run must
    not (a) flip its item back to done or (b) lose its tokens from the run total
    (the reaper rolled up before they were recorded). The final write locks the
    parent run, sees it terminal, discards the item, and re-rolls the run total."""
    session_factory, ws_id, wdir, repo, draft_sha = review_active_workspace
    settings = Settings()
    settings.workspaces_dir = str(wdir)
    with session_factory() as s:
        run = enqueue_review_run(s, workspace_id=ws_id, draft_sha=draft_sha)
        run.status = "running"
        s.add(ReviewRunItem(review_run_id=run.id, kind="fit_check", status="queued"))
        s.commit()
        run_id = run.id

    def builder(*, settings, session_factory, workspace_id, review_run_id, item_id, kind,
                lifecycle_generation):
        class _Fake:
            async def launch(self, ctx):
                return _Handle(workspace_id)

            async def send_turn(self, handle, instruction):
                # The model produced markdown (without the fix the late write would
                # flip this item to "done")...
                with session_factory() as s:
                    s.get(ReviewRunItem, item_id).raw_response_json = {"markdown": "# fit\nok"}
                    s.commit()
                # ...and the reaper fires mid-call, BEFORE this item recorded tokens,
                # so its rollup sees $0 for fit_check and nulls the run total.
                with session_factory() as s:
                    s.execute(
                        ReviewRun.__table__.update()
                        .where(ReviewRun.id == review_run_id)
                        .values(triggered_at=datetime(2000, 1, 1, tzinfo=UTC))
                    )
                    reap_stuck_review_runs(s, timeout_seconds=10)
                    s.commit()
                yield TurnDoneEvent(tokens_in=1000, tokens_out=500, model="claude-opus-4-7")

            def unconfirmed_launch_ids(self):
                return []

            async def retire_unconfirmed(self) -> int:
                return 0

            async def terminate(self, handle):
                return None

        return _Fake()

    await _run_one_call(
        kind="fit_check",
        instruction="x",
        run_id=run_id,
        workspace_id=ws_id,
        session_factory=session_factory,
        settings=settings,
        adapter_builder=builder,
        lifecycle_generation=0,
    )

    with session_factory() as s:
        run = s.get(ReviewRun, run_id)
        item = s.query(ReviewRunItem).filter_by(review_run_id=run_id, kind="fit_check").one()
        assert run.status == "failed"          # reaper's terminal state preserved
        assert item.status == "cancelled"      # late result discarded, not resurrected to done
        assert item.input_tokens == 1000       # tokens still recorded on the item
        # run total re-rolled to include the late spend (1000 in + 500 out -> $0.0175
        # -> $0.018 at run precision), so review_dollars_today still counts it
        assert run.estimated_dollars == Decimal("0.018")


@pytest.mark.asyncio
async def test_finalize_refuses_to_commit_when_the_workspace_moved_on(
    review_active_workspace, _settings_env
):
    """The review finalize is the ONLY production git-commit sink that takes no
    mutex and no fence (measured: zero occurrences of `branch`,
    `acquire_commit_mutex`, `acquire_shared_lease`, `live_at_generation` or any
    fencing import in review/pass_runner.py; the other three pass runners commit
    to git zero times).

    That matters more than "it commits to the wrong branch": `git commit` carries
    no pathspec (gitio.py:198), so it commits the WHOLE INDEX. An unmutexed
    writer can therefore hand the next checkpoint a commit it did not author.

    Here the consolidation sub-call bumps the workspace generation before
    returning — standing in for any concurrent lifecycle move (a branch switch, a
    delete, a retirement) that lands while the panel's six hosted calls are in
    flight. The finalize must then write NOTHING to git.
    """
    from grant_lab.db.models import Workspace

    session_factory, ws_id, wdir, repo, draft_sha = review_active_workspace
    run_id = _claim_running(session_factory, ws_id, draft_sha)
    settings = Settings()
    settings.workspaces_dir = str(wdir)

    head_before = repo_head(repo)

    inner = make_fake_builder(session_factory, {})

    def moving_builder(**kwargs):
        adapter = inner(**kwargs)
        if kwargs["kind"] == "consolidation":
            real_send = adapter.send_turn

            async def send_turn(handle, instruction):
                async for event in real_send(handle, instruction):
                    yield event
                # The workspace moves on AFTER the last hosted call and BEFORE
                # the finalize — the exact window the fence exists to cover.
                with session_factory() as s:
                    s.get(Workspace, ws_id).lifecycle_generation += 1
                    s.commit()

            adapter.send_turn = send_turn
        return adapter

    await run_review_pass(
        run_id=run_id,
        stamp=review_stamp(session_factory, run_id, 0),
        session_factory=session_factory,
        settings=settings,
        adapter_builder=moving_builder,
    )

    assert repo_head(repo) == head_before, "review finalize committed to a moved workspace"
    with session_factory() as s:
        run = s.get(ReviewRun, run_id)
        assert run.fit_check_sha is None
        assert run.rubric_review_sha is None
        assert run.status == "failed"


@pytest.mark.asyncio
async def test_a_stamp_naming_a_different_workspace_is_refused(
    review_active_workspace, _settings_env
):
    """The stamp names a workspace and so does the run; nothing downstream checks
    they agree.

    This is the same latent split `checkpoint_artifact` carries — it LOCKS
    `stamp.workspace_id` while reading and writing a separately-supplied
    `workspace_id` — and a mismatch here would fence one workspace and commit
    into another, which is the exact failure the fence exists to prevent.

    Written because the guard was added without one and a mutation proved it:
    replacing the condition with `if False` left the whole suite green.
    """
    import uuid as _uuid

    from grant_lab.proposal_assist.ports.stamp import FenceStamp

    session_factory, ws_id, wdir, repo, draft_sha = review_active_workspace
    run_id = _claim_running(session_factory, ws_id, draft_sha)
    settings = Settings()
    settings.workspaces_dir = str(wdir)

    other = FenceStamp(
        workspace_id=_uuid.uuid4(),
        generation=0,
        active_turn_job_id=None,
        origin_job_id=run_id,
    )
    with pytest.raises(ValueError, match="stamp names workspace"):
        await run_review_pass(
            run_id=run_id,
            stamp=other,
            session_factory=session_factory,
            settings=settings,
            adapter_builder=make_fake_builder(session_factory, {}),
        )


def test_the_finalize_takes_the_mutex_BEFORE_the_review_run_row():
    """Lock order M -> J inside the FINALIZE, asserted structurally because it
    cannot be asserted behaviourally without provoking a real deadlock.

    `fencing.py:139-145` is normative: the mutex must precede any job or workspace
    row lock. The delete path runs M -> J. If this finalize took the ReviewRun row
    FOR UPDATE first and the mutex second it would run J -> M — an AB-BA inversion
    and a genuine PostgreSQL 40P01 that no unit test would surface until it bit in
    production.

    Scoped to the git write on purpose. A first draft compared the mutex against
    the FIRST `with_for_update` in the whole function and failed on correct code:
    the PREFIX legitimately takes rows long before the finalize exists. The
    property that matters is about the pair guarding `write_and_commit_files`, so
    the anchors are the last of each BEFORE that call.

    A source-order assertion is weaker than a behavioural one and is stated as
    such. It is not nothing: it fails the moment someone moves the acquisition
    below the row lock, which is the only way this regresses.
    """
    import ast
    import inspect

    from grant_lab.proposal_assist.review import pass_runner as m

    tree = ast.parse(inspect.cleandoc(inspect.getsource(m.run_review_pass)))
    mutex, run_locks, git_writes = [], [], []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
        if name == "acquire_commit_mutex_at_generation":
            mutex.append(node.lineno)
        elif name == "write_and_commit_files":
            git_writes.append(node.lineno)
        elif name == "get" and any(kw.arg == "with_for_update" for kw in node.keywords):
            first = node.args[0] if node.args else None
            if isinstance(first, ast.Name) and first.id == "ReviewRun":
                run_locks.append(node.lineno)

    assert git_writes, "run_review_pass no longer commits to git — rewrite this gate"
    write = min(git_writes)
    before_write = [n for n in mutex if n < write]
    locks_before_write = [n for n in run_locks if n < write]
    assert before_write, "the git write is no longer preceded by the commit mutex"
    assert locks_before_write, "the finalize no longer row-locks the ReviewRun"
    assert max(before_write) < max(locks_before_write), (
        f"lock-order inversion: the mutex guarding the git write is taken at line "
        f"{max(before_write)} but the ReviewRun row is locked FOR UPDATE at line "
        f"{max(locks_before_write)} — M must precede J (fencing.py:139-145), or this "
        "finalize deadlocks against the delete path's M -> J"
    )
