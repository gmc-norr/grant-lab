import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from call_manager.db.models import Attachment, AttachmentJob  # merged
from tests.proposal_assist.conftest import make_user, make_workspace


def test_attachment_insert_figure(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        uploader = make_user(s, email="uploader@umu.se")
        s.add(
            Attachment(
                workspace_id=ws.id,
                kind="figure",
                filename="fig1.png",
                storage_path="figs/fig1.png",
                mime_type="image/png",
                uploaded_by=uploader.id,
            )
        )
        s.commit()
        a = s.scalar(select(Attachment).where(Attachment.workspace_id == ws.id))
        assert isinstance(a.id, uuid.UUID)
        assert a.kind == "figure"
        assert a.filename == "fig1.png"
        assert a.description is None  # populated later by describe_figure job


def test_attachment_kind_check(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        uploader = make_user(s, email="x@umu.se")
        s.add(
            Attachment(
                workspace_id=ws.id,
                kind="not_a_kind",
                filename="x",
                storage_path="x",
                mime_type="x/x",
                uploaded_by=uploader.id,
            )
        )
        with pytest.raises(IntegrityError):
            s.commit()


def _add_attachment(s, ws_id, user_id, *, filename="fig.png"):
    a = Attachment(
        workspace_id=ws_id,
        kind="figure",
        filename=filename,
        storage_path=f"figs/{filename}",
        mime_type="image/png",
        uploaded_by=user_id,
    )
    s.add(a)
    s.flush()
    return a


def test_attachment_job_insert_queued(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        uploader = make_user(s, email="u@umu.se")
        attachment = _add_attachment(s, ws.id, uploader.id)
        s.add(
            AttachmentJob(
                workspace_id=ws.id,
                attachment_id=attachment.id,
                kind="describe_figure",
                status="queued",
            )
        )
        s.commit()
        j = s.scalar(select(AttachmentJob).where(AttachmentJob.attachment_id == attachment.id))
        assert j is not None
        assert j.status == "queued"
        assert j.attempts == 0


def test_attachment_job_unique_active_per_attachment_kind(pg_session_factory):
    """Partial UNIQUE on (attachment_id, kind) WHERE status IN
    ('queued','running') prevents double-enqueueing the same describe job."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        uploader = make_user(s, email="u2@umu.se")
        attachment = _add_attachment(s, ws.id, uploader.id)
        s.add(
            AttachmentJob(
                workspace_id=ws.id,
                attachment_id=attachment.id,
                kind="describe_figure",
                status="queued",
            )
        )
        s.commit()
        s.add(
            AttachmentJob(
                workspace_id=ws.id,
                attachment_id=attachment.id,
                kind="describe_figure",
                status="running",
            )
        )
        with pytest.raises(IntegrityError):
            s.commit()


def test_attachment_job_completed_allows_re_enqueue(pg_session_factory):
    """A previously-completed job doesn't block a new queued job for the
    same (attachment_id, kind) — the partial index is bounded by status."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        uploader = make_user(s, email="u3@umu.se")
        attachment = _add_attachment(s, ws.id, uploader.id)
        s.add(
            AttachmentJob(
                workspace_id=ws.id,
                attachment_id=attachment.id,
                kind="describe_figure",
                status="completed",
            )
        )
        s.commit()
        s.add(
            AttachmentJob(
                workspace_id=ws.id,
                attachment_id=attachment.id,
                kind="describe_figure",
                status="queued",
            )
        )
        s.commit()  # should not raise


def test_attachment_job_updated_at_bumped_on_modification(pg_session_factory):
    """onupdate=func.now() on AttachmentJob.updated_at keeps the
    `ix_attachment_jobs_status_updated` index meaningful — without it, a
    job worker would have to manually set updated_at on every status
    transition, which is easy to forget and would mislead the index."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        uploader = make_user(s, email="updated-at@umu.se")
        attachment = _add_attachment(s, ws.id, uploader.id)
        job = AttachmentJob(
            workspace_id=ws.id,
            attachment_id=attachment.id,
            kind="describe_figure",
            status="queued",
        )
        s.add(job)
        s.commit()
        original = job.updated_at
        job.status = "running"
        s.commit()
        s.refresh(job)
        assert job.updated_at > original
