"""Unit tests for the two workspace service functions.

`soft_delete_workspace_db` backs the JSON DELETE /api/workspaces/{id} endpoint.
`scrub_workspace_bytes` is reached by no route — Slice A's freeze-only delete
stopped calling it — so this file is its only coverage until Slice E's admin
purge gives it a caller again."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from grant_lab.db.models import (
    ChatTurn,
    CoherenceJob,
    EvaluatorJob,
    ReviewRun,
    TurnJob,
    UncitedClaimsJob,
    Workspace,
    WorkspaceEvent,
)
from grant_lab.proposal_assist.attachments.storage import AttachmentStorage
from grant_lab.proposal_assist.references.storage import ReferenceStorage
from grant_lab.workspaces import scrub_workspace_bytes
from tests.proposal_assist.conftest import make_workspace
from tests.workspace_delete_helper import soft_delete_workspace_for_test


def test_soft_delete_cascades_and_keeps_bytes(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(CoherenceJob(workspace_id=ws.id, status="queued"))
        s.add(EvaluatorJob(workspace_id=ws.id, status="queued"))
        # ReviewRun.draft_sha is NOT NULL with no server_default — seed it (matches
        # tests/proposal_assist/review/test_soft_delete_cancels_review.py).
        s.add(ReviewRun(workspace_id=ws.id, status="queued", draft_sha="a" * 64))
        s.add(UncitedClaimsJob(workspace_id=ws.id, status="queued"))
        ut = ChatTurn(workspace_id=ws.id, role="user", content={"type": "text", "text": "hi"})
        s.add(ut)
        s.flush()
        s.add(
            TurnJob(
                workspace_id=ws.id,
                user_turn_id=ut.id,
                submitted_by_user_id=ws.owner_id,
                status="queued",
                queued_at=datetime.now(UTC),
            )
        )
        s.commit()
        ws_id = ws.id

    # Seed an on-disk attachment byte tree so we can assert delete leaves it alone.
    attach_dir = tmp_path / str(ws_id) / "attachments"
    attach_dir.mkdir(parents=True)
    (attach_dir / "blob.bin").write_bytes(b"x")

    with pg_session_factory() as s:
        soft_delete_workspace_for_test(s, workspace_id=ws_id)

    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).soft_deleted_at is not None
        assert (
            s.scalar(select(CoherenceJob.status).where(CoherenceJob.workspace_id == ws_id))
            == "failed"
        )
        assert (
            s.scalar(select(EvaluatorJob.status).where(EvaluatorJob.workspace_id == ws_id))
            == "failed"
        )
        # NB: review's cancel helper sets "cancelled" (not "failed" like the other three).
        assert (
            s.scalar(select(ReviewRun.status).where(ReviewRun.workspace_id == ws_id))
            == "cancelled"
        )
        assert (
            s.scalar(
                select(UncitedClaimsJob.status).where(UncitedClaimsJob.workspace_id == ws_id)
            )
            == "failed"
        )
        assert (
            s.scalar(select(TurnJob.status).where(TurnJob.workspace_id == ws_id)) == "cancelled"
        )
        # emit_chat=False → audit event but NO system chat row.
        assert (
            s.scalar(
                select(WorkspaceEvent.id).where(WorkspaceEvent.kind == "cloud_turn_cancelled")
            )
            is not None
        )
        assert s.scalar(select(ChatTurn).where(ChatTurn.role == "system")) is None
    assert attach_dir.exists(), (
        "Slice A: deleting a workspace FREEZES it and destroys nothing, so a "
        "later restore is lossless. Only an admin purge removes bytes."
    )


def test_soft_delete_terminalizes_running_turn_and_finalize_is_noop(pg_session_factory):
    from grant_lab.db.models import ChatTurn, TurnJob
    from grant_lab.proposal_assist.turns.processor import _finalize_success
    from tests.proposal_assist.conftest import make_chat_turn, make_workspace
    from tests.workspace_delete_helper import soft_delete_workspace_for_test

    with pg_session_factory() as s:
        ws = make_workspace(s)
        user_turn = make_chat_turn(
            s, ws, role="user", content={"type": "text", "text": "q"}, tokens_in=1234
        )
        job = TurnJob(
            workspace_id=ws.id,
            status="running",
            user_turn_id=user_turn.id,
            submitted_by_user_id=ws.owner_id,
        )
        s.add(job)
        s.commit()
        ws_id, job_id, ut_id = ws.id, job.id, user_turn.id

    with pg_session_factory() as s:
        soft_delete_workspace_for_test(s, workspace_id=ws_id)

    with pg_session_factory() as s:
        assert s.get(TurnJob, job_id).status != "running"  # terminalized in the delete txn
        assert s.get(ChatTurn, ut_id).tokens_in == 1234  # estimate retained

    # The in-flight processor now finalizes "successfully" — must be a NO-OP.
    _finalize_success(
        pg_session_factory,
        turn_job_id=job_id,
        workspace_id=ws_id,
        submitted_by_user_id=None,
        user_turn_id=ut_id,
        last_assistant_id=None,
        tokens_in=10,
        tokens_out=5,
        model="m",
        session_id=None,
    )

    with pg_session_factory() as s:
        assert s.get(TurnJob, job_id).status != "completed"  # never resurrected


def test_soft_delete_refuses_a_second_deletion(pg_session_factory):
    """The delete's own under-mutex liveness read: `ws` is loaded BEFORE we can
    block on the mutex, so a concurrent deletion that committed while we waited
    would be invisible to it. Sequential here; the concurrent form is the route
    test."""
    from grant_lab.proposal_assist.drafting.locking import WorkspaceDeleted
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id

    with pg_session_factory() as s:
        soft_delete_workspace_for_test(s, workspace_id=ws_id)
    with pg_session_factory() as s:
        with pytest.raises(WorkspaceDeleted):
            soft_delete_workspace_for_test(s, workspace_id=ws_id)
        s.rollback()


def test_soft_delete_propagates_commit_mutex_timeout_without_committing(
    pg_session_factory,
):
    """A wedged writer holding the mutex must make the delete FAIL cleanly, not
    half-apply: nothing committed, the workspace still live."""
    import threading

    from grant_lab.proposal_assist.drafting.locking import (
        CommitMutexTimeout,
        acquire_commit_mutex,
    )
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id

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
    try:
        holding.wait(timeout=5)
        with pg_session_factory() as s:
            with pytest.raises(CommitMutexTimeout):
                soft_delete_workspace_for_test(
                    s, workspace_id=ws_id, timeout_seconds=1
                )
            s.rollback()
    finally:
        release.set()
        t.join(timeout=30)

    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).soft_deleted_at is None  # nothing committed


def test_the_db_half_stamps_terminalizes_and_refuses_a_redelete(pg_session_factory):
    """soft_delete_workspace_db alone: the soft_deleted_at stamp, running-turn
    terminalization, and WorkspaceDeleted on a re-delete.

    This used to compare the halves against the soft_delete_workspace wrapper.
    Slice C2 §9 deleted that wrapper — it composed the halves while skipping
    retirement entirely — so the comparison is gone and the assertions it made
    about the halves stay here."""
    from grant_lab.proposal_assist.drafting.locking import WorkspaceDeleted
    from grant_lab.workspaces import soft_delete_workspace_db
    from tests.proposal_assist.conftest import make_chat_turn, make_user, make_workspace

    # The DB half, run explicitly — no route calls it in isolation.
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="split-a@umu.se"))
        ut = make_chat_turn(s, ws, role="user", content={"type": "text", "text": "q"},
                            tokens_in=1234)
        job = TurnJob(workspace_id=ws.id, status="running",
                      user_turn_id=ut.id, submitted_by_user_id=ws.owner_id)
        s.add(job)
        s.commit()
        split_id, job_id, ut_id = ws.id, job.id, ut.id

    with pg_session_factory() as s:
        soft_delete_workspace_db(s, workspace_id=split_id)

    with pg_session_factory() as s:
        assert s.get(Workspace, split_id).soft_deleted_at is not None
        assert s.get(TurnJob, job_id).status != "running"      # terminalized
        assert s.get(ChatTurn, ut_id).tokens_in == 1234        # estimate retained
    # DB half is idempotent-refusing: a re-delete raises rather than re-stamping.
    with pg_session_factory() as s:
        with pytest.raises(WorkspaceDeleted):
            soft_delete_workspace_db(s, workspace_id=split_id)
        s.rollback()


def test_the_production_soft_delete_wrapper_stays_deleted():
    """Slice C2 §9. The wrapper composed the DB and FS halves WITHOUT retirement,
    while looking exactly like the entry point you would reach for. If it comes
    back, every caller silently skips pass, job and collab-room teardown."""
    import grant_lab.workspaces as ws_mod

    assert not hasattr(ws_mod, "soft_delete_workspace"), (
        "soft_delete_workspace is back in production. It skips retirement. Use "
        "tests.workspace_delete_helper.soft_delete_workspace_for_test in tests, or "
        "call soft_delete_workspace_db with retirement after it (see "
        "web/api/workspaces.py's delete route) — scrub_workspace_bytes is not "
        "part of that composition; production has no caller for it today."
    )


def test_scrub_removes_uploads_and_leaves_the_repo_alone(tmp_path):
    """`scrub_workspace_bytes` has no production caller — Slice E's purge will be
    the first. Pin the composition here so it cannot rot in the meantime: both
    upload subtrees go, `repo/` stays, and a sibling workspace is untouched."""
    ws_id, other_id = uuid.uuid4(), uuid.uuid4()
    for wid in (ws_id, other_id):
        tree = (("attachments", "blob.bin"), ("references", "paper.pdf"), ("repo", "draft.md"))
        for sub, name in tree:
            d = tmp_path / str(wid) / sub
            d.mkdir(parents=True)
            (d / name).write_bytes(b"real bytes")

    scrub_workspace_bytes(ws_id, workspaces_dir=tmp_path)

    assert not (tmp_path / str(ws_id) / "attachments").exists()
    assert not (tmp_path / str(ws_id) / "references").exists()
    # The git-backed draft is NOT an upload — purge scope must not widen by accident.
    assert (tmp_path / str(ws_id) / "repo" / "draft.md").read_bytes() == b"real bytes"
    # Scoped to one workspace.
    assert (tmp_path / str(other_id) / "attachments" / "blob.bin").read_bytes() == b"real bytes"


def test_scrub_propagates_oserror_so_a_partial_removal_cannot_look_like_success(
    tmp_path, monkeypatch
):
    """Retention spec §9 (Slice E): a partial removal must surface as a controlled
    retryable failure committing no tombstone and no audit event. If this swallowed
    the error, a purge would stamp "purged" over bytes still on disk and nothing
    downstream could tell the difference. Pinned here because Slice E is the only
    intended caller and it does not exist yet."""
    ws_id = uuid.uuid4()

    def boom(self, *, workspace_id):
        raise OSError("permission denied")

    monkeypatch.setattr(ReferenceStorage, "delete_workspace", boom)
    attach_dir = tmp_path / str(ws_id) / "attachments"
    attach_dir.mkdir(parents=True)
    (attach_dir / "blob.bin").write_bytes(b"real bytes")

    with pytest.raises(OSError, match="permission denied"):
        scrub_workspace_bytes(ws_id, workspaces_dir=tmp_path)

    # Partial: the first subtree is already gone. A retry must finish the job,
    # which it can only do because each subtree delete no-ops when absent.
    assert not attach_dir.exists()
    monkeypatch.undo()
    scrub_workspace_bytes(ws_id, workspaces_dir=tmp_path)


def test_scrub_raises_before_the_second_subtree_when_the_first_fails(tmp_path, monkeypatch):
    """Ordering matters for the retry contract: the reference subtree must survive
    an attachment failure rather than being removed by a half-finished pass."""
    ws_id = uuid.uuid4()
    ref_dir = tmp_path / str(ws_id) / "references"
    ref_dir.mkdir(parents=True)
    (ref_dir / "paper.pdf").write_bytes(b"real bytes")

    def boom(self, *, workspace_id):
        raise OSError("disk error")

    monkeypatch.setattr(AttachmentStorage, "delete_workspace", boom)

    with pytest.raises(OSError, match="disk error"):
        scrub_workspace_bytes(ws_id, workspaces_dir=tmp_path)

    assert (ref_dir / "paper.pdf").read_bytes() == b"real bytes"
