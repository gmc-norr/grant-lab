import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from grant_lab.db.models import ArtifactVersion, ChatTurn
from tests.proposal_assist.conftest import make_workspace


def test_artifact_version_insert_agent_commit(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        triggering = ChatTurn(
            workspace_id=ws.id,
            role="assistant",
            content={"text": "edit"},
        )
        s.add(triggering)
        s.flush()
        s.add(
            ArtifactVersion(
                workspace_id=ws.id,
                commit_sha="a" * 40,
                triggering_turn_id=triggering.id,
                created_by="agent",
                message="agent: edit proposal section 2",
            )
        )
        s.commit()
        v = s.scalar(select(ArtifactVersion).where(ArtifactVersion.workspace_id == ws.id))
        assert isinstance(v.id, uuid.UUID)
        assert v.commit_sha == "a" * 40
        assert v.created_by == "agent"
        assert v.triggering_turn_id == triggering.id


def test_artifact_version_triggering_turn_id_nullable(pg_session_factory):
    """System-driven commits (e.g., from a recovery script) have no turn."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(
            ArtifactVersion(
                workspace_id=ws.id,
                commit_sha="b" * 40,
                triggering_turn_id=None,
                created_by="system",
                message="system: initial commit",
            )
        )
        s.commit()
        v = s.scalar(select(ArtifactVersion).where(ArtifactVersion.workspace_id == ws.id))
        assert v.triggering_turn_id is None


def test_artifact_version_created_by_check(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(
            ArtifactVersion(
                workspace_id=ws.id,
                commit_sha="c" * 40,
                created_by="not_a_role",
                message="x",
            )
        )
        with pytest.raises(IntegrityError):
            s.commit()
