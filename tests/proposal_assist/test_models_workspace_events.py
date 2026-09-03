import uuid
from datetime import datetime

from sqlalchemy import select

from call_manager.db.models import WorkspaceEvent
from tests.proposal_assist.conftest import make_user, make_workspace


def test_workspace_event_insert_with_payload(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        actor = make_user(s, email="actor@umu.se")
        s.add(
            WorkspaceEvent(
                workspace_id=ws.id,
                kind="created",
                payload={"title": ws.title, "owner_email": "actor@umu.se"},
                actor_id=actor.id,
            )
        )
        s.commit()
        ev = s.scalar(select(WorkspaceEvent).where(WorkspaceEvent.workspace_id == ws.id))
        assert ev is not None
        assert isinstance(ev.id, uuid.UUID)
        assert ev.kind == "created"
        assert ev.payload["title"] == ws.title
        assert ev.actor_id == actor.id
        assert isinstance(ev.created_at, datetime)


def test_workspace_event_actor_id_is_nullable(pg_session_factory):
    """System events (e.g., scheduler-driven) have no actor."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(
            WorkspaceEvent(
                workspace_id=ws.id,
                kind="status_changed",
                payload={"from": "drafting", "to": "submitted"},
                actor_id=None,
            )
        )
        s.commit()
        ev = s.scalar(select(WorkspaceEvent).where(WorkspaceEvent.workspace_id == ws.id))
        assert ev.actor_id is None
