"""Server-side enforcement of the cloud-LLM data-disclosure acknowledgement.

Spec 2026-06-10 §9: the disclosure ack is enforced in post_turn, not only by
the UI modal. A direct POST (curl / replayed HTMX) that skips the modal must
NOT reach the cloud LLM. These tests pin that the /turns endpoint itself
refuses an un-acknowledged turn.
"""

from __future__ import annotations

from sqlalchemy import func, select

from call_manager.db.models import TurnJob
from tests.proposal_assist.conftest import make_workspace


def test_turn_blocked_without_disclosure_ack(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    """An owner who has not acknowledged the disclosure is refused (403) and
    no TurnJob is enqueued — the cloud LLM is never reached."""
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/turns",
        data={"content": "Hello agent"},
    )

    assert response.status_code == 403
    with pg_session_factory() as s:
        n_jobs = s.scalar(
            select(func.count()).select_from(TurnJob).where(TurnJob.workspace_id == wsid)
        )
        assert n_jobs == 0


def test_turn_allowed_after_disclosure_ack(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    """After acknowledging the disclosure, the same owner's turn is no longer
    blocked by the gate and a TurnJob is enqueued."""
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    ack = client_authed_owner.post(f"/workspaces/{wsid}/disclosure")
    assert ack.status_code == 200, ack.text

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/turns",
        data={"content": "Hello agent"},
    )

    assert response.status_code in (200, 201, 303)
    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.workspace_id == wsid))
        assert job is not None
        assert job.status == "queued"
