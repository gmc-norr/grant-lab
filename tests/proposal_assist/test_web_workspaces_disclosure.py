"""Task 12: per-user, per-workspace first-time disclosure modal."""


def test_main_view_shows_disclosure_modal_on_first_visit(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    """A workspace with no disclosure_acknowledged event for this user
    renders the modal."""
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id
    response = client_authed_owner.get(f"/workspaces/{wsid}")
    assert response.status_code == 200
    assert b"disclosure-modal" in response.content
    # Spec language must appear so the user actually reads what they're
    # consenting to.
    assert b"Anthropic" in response.content


def test_main_view_hides_modal_after_acknowledgment(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    """After a successful POST /disclosure, subsequent renders skip
    the modal."""
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    ack = client_authed_owner.post(f"/workspaces/{wsid}/disclosure")
    assert ack.status_code == 200

    response = client_authed_owner.get(f"/workspaces/{wsid}")
    assert response.status_code == 200
    assert b"disclosure-modal" not in response.content


def test_post_disclosure_writes_workspace_event(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    from sqlalchemy import select

    from call_manager.db.models import WorkspaceEvent
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        wsid = ws.id

    response = client_authed_owner.post(f"/workspaces/{wsid}/disclosure")
    assert response.status_code == 200

    with pg_session_factory() as s:
        events = list(
            s.scalars(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.workspace_id == wsid,
                    WorkspaceEvent.kind == "disclosure_acknowledged",
                    WorkspaceEvent.actor_id == current_user.id,
                )
            )
        )
    assert len(events) == 1


def test_disclosure_is_per_user(
    client_authed_user,
    pg_session_factory,
    current_user,
):
    """If a different user has already acknowledged for this workspace,
    the current user still sees the modal."""
    from call_manager.db.models import WorkspaceEvent, WorkspaceViewer
    from tests.proposal_assist.conftest import make_user, make_workspace

    with pg_session_factory() as s:
        owner = make_user(s, email="o@ki.se")
        ws = make_workspace(s, owner=owner)
        s.flush()
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=current_user.id))
        # Owner has already acknowledged — current_user (a viewer) has NOT.
        s.add(
            WorkspaceEvent(
                workspace_id=ws.id,
                kind="disclosure_acknowledged",
                actor_id=owner.id,
                payload={},
            )
        )
        s.commit()
        wsid = ws.id

    response = client_authed_user.get(f"/workspaces/{wsid}")
    assert response.status_code == 200
    # current_user (viewer) still sees the modal — the owner's ack
    # doesn't apply to them.
    assert b"disclosure-modal" in response.content
