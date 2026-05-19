import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from call_monitor.db.models import Attachment
from tests.proposal_assist.conftest import make_user, make_workspace


def test_attachment_insert_figure(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        uploader = make_user(s, email="uploader@ki.se")
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
        uploader = make_user(s, email="x@ki.se")
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
