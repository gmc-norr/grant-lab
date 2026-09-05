import logging
import threading
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from grant_lab.db.models import ArtifactVersion, CollabArtifactState, Workspace, WorkspaceEvent
from grant_lab.proposal_assist.artifacts import PROPOSAL_FILENAME
from grant_lab.proposal_assist.collab import checkpoint as cp
from grant_lab.proposal_assist.collab import handover, state, store
from grant_lab.proposal_assist.collab.markdown_bridge import markdown_to_pm
from grant_lab.proposal_assist.collab.permissions import make_checkpoint_on_evict
from grant_lab.proposal_assist.collab.yjs_bridge import pm_json_to_ydoc
from tests.proposal_assist.conftest import init_workspace_repo, make_user, make_workspace


def _seed_edit(pg_session_factory, wid, actor_id, md) -> None:
    doc = pm_json_to_ydoc(markdown_to_pm(md))
    with pg_session_factory() as s:
        store.append_update(
            s, workspace_id=wid, artifact="draft", actor_id=actor_id, update=doc.get_update()
        )
        s.commit()


def _mk(pg_session_factory, tmp_path, email, md, *, soft_deleted=False):
    with pg_session_factory() as s:
        o = make_user(s, email=email)
        ws = make_workspace(s, owner=o)
        s.commit()
        wid, oid = ws.id, o.id
    init_workspace_repo(Path(tmp_path) / str(wid) / "repo", "## Background\noriginal.\n")
    with pg_session_factory() as s:
        state.enable_collab(s, workspace_id=wid, artifact="draft")
        if soft_deleted:
            s.get(Workspace, wid).soft_deleted_at = datetime.now(UTC)
        s.commit()
    _seed_edit(pg_session_factory, wid, oid, md)
    return wid


def test_checkpoint_active_tails_quietly_checkpoints_candidate(pg_session_factory, tmp_path):
    wid = _mk(pg_session_factory, tmp_path, "cat-a@umu.se", "## Background\n\nedited by A.\n")

    n = cp.checkpoint_active_tails(pg_session_factory, workspaces_dir=str(tmp_path), min_tail=1)

    assert n == 1
    disk = (Path(tmp_path) / str(wid) / "repo" / PROPOSAL_FILENAME).read_text()
    assert "edited by A." in disk                       # committed to git
    with pg_session_factory() as s:
        assert (
            s.execute(select(ArtifactVersion).where(ArtifactVersion.workspace_id == wid))
            .scalars()
            .all()
            == []
        )
        assert (
            s.execute(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.workspace_id == wid,
                    WorkspaceEvent.kind == "collab_checkpoint",
                )
            )
            .scalars()
            .all()
            == []
        )                                                # QUIET
        assert state.get_checkpoint_watermark(s, workspace_id=wid, artifact="draft") == 1


def test_checkpoint_active_tails_rechecks_soft_delete(pg_session_factory, tmp_path, monkeypatch):
    # A candidate that is soft-deleted between scan and checkpoint must be skipped by
    # the per-candidate recheck (mirrors make_checkpoint_on_evict). Force the finder to
    # return a soft-deleted workspace to exercise the recheck directly.
    wid = _mk(pg_session_factory, tmp_path, "cat-sd@umu.se", "## X\n\nedited.\n", soft_deleted=True)
    monkeypatch.setattr(
        store, "find_checkpoint_candidates", lambda s, *, min_tail: [(wid, "draft")]
    )

    n = cp.checkpoint_active_tails(pg_session_factory, workspaces_dir=str(tmp_path), min_tail=1)

    assert n == 0
    disk = (Path(tmp_path) / str(wid) / "repo" / PROPOSAL_FILENAME).read_text()
    assert disk == "## Background\noriginal.\n"          # NOT committed
    with pg_session_factory() as s:
        assert state.get_checkpoint_watermark(s, workspace_id=wid, artifact="draft") == 0


def test_checkpoint_active_tails_isolates_a_failing_artifact(
    pg_session_factory, tmp_path, monkeypatch
):
    wid_bad = _mk(pg_session_factory, tmp_path, "cat-bad@umu.se", "## X\n\nbad.\n")
    wid_ok = _mk(pg_session_factory, tmp_path, "cat-ok@umu.se", "## Y\n\nok.\n")
    real = cp.checkpoint_artifact

    def _flaky(session, *, workspace_id, **kw):
        if workspace_id == wid_bad:
            raise RuntimeError("boom")
        return real(session, workspace_id=workspace_id, **kw)

    monkeypatch.setattr(cp, "checkpoint_artifact", _flaky)

    n = cp.checkpoint_active_tails(pg_session_factory, workspaces_dir=str(tmp_path), min_tail=1)

    assert n == 1                                        # the healthy one still ran
    ok_disk = (Path(tmp_path) / str(wid_ok) / "repo" / PROPOSAL_FILENAME).read_text()
    assert "ok." in ok_disk
    bad_disk = (Path(tmp_path) / str(wid_bad) / "repo" / PROPOSAL_FILENAME).read_text()
    assert bad_disk == "## Background\noriginal.\n"      # failed one rolled back, uncommitted


def _hold_state_row(pg_session_factory, wid, artifact="draft"):
    """Park a FOR UPDATE on the collab_artifact_state row advance_checkpoint
    updates; return (release_event, thread).

    Deliberately NOT the workspace row — acquire_commit_mutex_live's liveness
    re-read is a plain SELECT and takes no row lock, so holding the workspace row
    would let the checkpoint sail straight through.
    """
    holding = threading.Event()
    release = threading.Event()

    def _holder():
        with pg_session_factory() as s:
            s.execute(
                select(CollabArtifactState)
                .where(
                    CollabArtifactState.workspace_id == wid,
                    CollabArtifactState.artifact == artifact,
                )
                .with_for_update()
            ).first()
            holding.set()
            # 15s, not 60: with the tail bounded the waiter gives up after ~2s. If
            # the bound is ever removed this must FAIL in a sane time rather than
            # look like an infrastructure hang.
            release.wait(timeout=15)
            s.rollback()

    t = threading.Thread(target=_holder)
    t.start()
    assert holding.wait(timeout=10), "the holder never took the state row"
    return release, t


def test_a_bounded_checkpoint_failure_leaves_the_tail_for_the_next_scan(
    pg_session_factory, tmp_path
):
    """checkpoint_active_tails is a periodic scheduler job. With the tail bounded
    its work can now raise; spec section 4 claims that is safe because a rolled-back
    attempt never advances the watermark, so find_checkpoint_candidates re-selects
    the same room on the next scan.

    Asserted as "still a candidate, watermark unmoved" rather than "it logged" — a
    job that logs and drops the work would satisfy the weaker claim just as well.
    """
    wid = _mk(pg_session_factory, tmp_path, "c3-absorb@umu.se", "## Background\n\nedited.\n")

    with pg_session_factory() as s:
        assert (wid, "draft") in store.find_checkpoint_candidates(s, min_tail=1)
        before = state.get_checkpoint_watermark(s, workspace_id=wid, artifact="draft")

    release, t = _hold_state_row(pg_session_factory, wid)
    try:
        # Must NOT raise out of the scan: errors are per-candidate and swallowed
        # (checkpoint.py's `except Exception` in the scan loop). The bounded tail
        # turns what was a hang into one of those.
        done = cp.checkpoint_active_tails(
            pg_session_factory, workspaces_dir=str(tmp_path), min_tail=1
        )
    finally:
        release.set()
        t.join(timeout=30)

    assert done == 0, "a contended candidate must not be counted as checkpointed"

    with pg_session_factory() as s:
        assert (wid, "draft") in store.find_checkpoint_candidates(s, min_tail=1), (
            "a failed checkpoint advanced the watermark anyway — the tail would now be "
            "skipped forever instead of retried on the next scan"
        )
        assert state.get_checkpoint_watermark(s, workspace_id=wid, artifact="draft") == before, (
            "the rolled-back attempt moved the watermark"
        )


async def test_a_bounded_eviction_checkpoint_logs_skipped_not_error(
    pg_session_factory, tmp_path, caplog
):
    """make_checkpoint_on_evict reaches the commit mutex INDIRECTLY, through
    checkpoint_artifact, which is why the slice's site inventory — built from
    direct acquire_commit_mutex* call sites — missed it.

    Its benign arm catches (GitCommitError, CommitMutexTimeout, WorkspaceDeleted)
    by TYPE. A bounded tail raises a raw OperationalError, so before this fix
    ordinary row contention on room eviction logged an ERROR with a full
    traceback instead of the benign 'skipped' WARNING.

    Contends the collab_artifact_state row advance_checkpoint updates — NOT the
    workspace row, whose only reader here is a plain SELECT that takes no lock.
    """
    wid = _mk(pg_session_factory, tmp_path, "c3-evict@umu.se", "## Background\n\nedited.\n")
    on_evict = make_checkpoint_on_evict(pg_session_factory, str(tmp_path), wid, "draft")

    release, t = _hold_state_row(pg_session_factory, wid)
    try:
        with caplog.at_level(logging.WARNING):
            await on_evict(None)
    finally:
        release.set()
        t.join(timeout=30)

    events = [r.__dict__.get("event") for r in caplog.records]
    assert "collab_checkpoint_on_evict_skipped" in events, (
        f"a bounded eviction checkpoint should log the benign skip; got {events}"
    )
    assert "collab_checkpoint_on_evict_error" not in events, (
        "routine row contention logged an ERROR with a traceback — this is the "
        "regression Slice C3 introduced at the 14th, indirect mutex site"
    )


def _records(caplog, event):
    return [r for r in caplog.records if r.__dict__.get("event") == event]


def test_a_bounded_scan_checkpoint_logs_a_warning_without_a_traceback(
    pg_session_factory, tmp_path, caplog
):
    """checkpoint_active_tails is a periodic scheduler job that ticks forever.
    Since Slice C3 bounded the mutex-held tail, ordinary row contention reaches
    its `except Exception` as a raw OperationalError(55P03) instead of hanging.

    WARNING is the right level either way — the attempt rolled back and the tail
    is re-selected next scan — but the traceback is not: an error-shaped stack
    trace per tick for routine contention is exactly the noise that trains
    operators to ignore this log line. Contends the collab_artifact_state row
    advance_checkpoint writes; the workspace row would prove nothing, since
    acquire_commit_mutex_live's liveness re-read is a plain SELECT.
    """
    wid = _mk(pg_session_factory, tmp_path, "c3-scanlog@umu.se", "## Background\n\nedited.\n")

    release, t = _hold_state_row(pg_session_factory, wid)
    try:
        with caplog.at_level(logging.WARNING):
            done = cp.checkpoint_active_tails(
                pg_session_factory, workspaces_dir=str(tmp_path), min_tail=1
            )
    finally:
        release.set()
        t.join(timeout=30)

    assert done == 0, "a contended candidate must not be counted as checkpointed"
    recs = _records(caplog, "collab_checkpoint_tail_failed")
    assert len(recs) == 1, (
        "the contended candidate should log exactly one benign warning; got "
        f"{[r.__dict__.get('event') for r in caplog.records]}"
    )
    assert recs[0].levelno == logging.WARNING
    # `exc_info=False` records as False, not None — assert falsy so the check is
    # about "no traceback attached", not about which falsy value logging stored.
    assert not recs[0].exc_info, (
        "routine bounded-tail contention emitted a full traceback from a job that "
        "runs continuously"
    )


def test_a_real_scan_failure_keeps_its_traceback(pg_session_factory, tmp_path, monkeypatch, caplog):
    """The other half of the contract above: suppressing the traceback is scoped
    to is_lock_timeout ONLY. A genuine failure on this best-effort background
    path is diagnosable from nothing but this log line, so it must keep exc_info.
    """
    wid = _mk(pg_session_factory, tmp_path, "c3-scanreal@umu.se", "## Background\n\nedited.\n")

    def _boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(cp, "checkpoint_artifact", _boom)

    with caplog.at_level(logging.WARNING):
        done = cp.checkpoint_active_tails(
            pg_session_factory, workspaces_dir=str(tmp_path), min_tail=1
        )

    assert done == 0
    recs = _records(caplog, "collab_checkpoint_tail_failed")
    assert len(recs) == 1
    assert recs[0].exc_info is not None, (
        "a real failure lost its traceback — the suppression is meant to be scoped "
        f"to lock timeouts (workspace {wid})"
    )


def test_a_bounded_handover_checkpoint_logs_skipped_not_error(
    pg_session_factory, tmp_path, caplog
):
    """run_kill_switch_handover reaches the commit mutex INDIRECTLY, through
    checkpoint_artifact — the same blind spot that hid make_checkpoint_on_evict
    from the slice's direct-call-site inventory.

    Its catch-all logged ERROR with a traceback, so once the tail was bounded the
    kill-switch rollback path emitted an error-level stack trace for ordinary row
    contention — on a pass the scheduler re-runs every tick while the switch is
    engaged. Contention is benign here: the log is NOT truncated, so the next
    tick retries.
    """
    wid = _mk(pg_session_factory, tmp_path, "c3-handover@umu.se", "## Background\n\nedited.\n")

    release, t = _hold_state_row(pg_session_factory, wid)
    try:
        with caplog.at_level(logging.WARNING):
            done = handover.run_kill_switch_handover(
                pg_session_factory, workspaces_dir=str(tmp_path)
            )
    finally:
        release.set()
        t.join(timeout=30)

    assert done == 0, "a contended artifact must not be counted as handed over"
    events = [r.__dict__.get("event") for r in caplog.records]
    assert "collab_kill_switch_handover_skipped" in events, (
        f"a bounded handover checkpoint should log the benign skip; got {events}"
    )
    assert "collab_kill_switch_handover_failed" not in events, (
        "routine row contention logged an ERROR with a traceback on the kill-switch "
        "rollback path — the 15th, indirect mutex site"
    )
    rec = _records(caplog, "collab_kill_switch_handover_skipped")[0]
    assert rec.levelno == logging.WARNING
    assert rec.exc_info is None, "the benign arm must not carry a traceback"
    with pg_session_factory() as s:
        assert store.load_rows(s, workspace_id=wid, artifact="draft"), (
            "the handover truncated the log despite failing to hand it over"
        )
