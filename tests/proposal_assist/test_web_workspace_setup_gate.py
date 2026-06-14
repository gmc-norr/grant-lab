"""Phase-0 setup gate (Theme 7 slice A).

While current_phase == 'setup': POST /turns is rejected (409), the main view
shows the setup-status card instead of the composer, and the card self-polls
GET /workspaces/{id}/setup-status until the WorkspaceSetupJob is terminal.
"""

from __future__ import annotations

import pytest


def test_post_turn_during_setup_returns_409(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    from sqlalchemy import select

    from call_manager.db.models import TurnJob
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=True, current_phase="setup")
        s.commit()
        wsid = ws.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/turns",
        data={"content": "too early"},
    )
    assert response.status_code == 409
    assert "still setting up" in response.json()["detail"]
    with pg_session_factory() as s:
        assert s.scalar(select(TurnJob).where(TurnJob.workspace_id == wsid)) is None


def test_post_turn_after_setup_proceeds(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=True)  # spec phase
        s.commit()
        wsid = ws.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/turns",
        data={"content": "hello"},
    )
    assert response.status_code in (200, 201, 303)


def _make_setup_workspace(pg_session_factory, owner, *, job_status=None, last_error=None):
    """A setup-phase workspace, optionally with a WorkspaceSetupJob row.

    make_workspace doesn't enqueue a setup job (the creation route does), so
    tests control the job row explicitly. job_status=None ⇒ no row (the
    endpoint falls back to the polling 'queued' card).
    """
    from call_manager.db.models import WorkspaceSetupJob
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=owner, current_phase="setup")
        s.flush()
        if job_status is not None:
            s.add(
                WorkspaceSetupJob(
                    workspace_id=ws.id,
                    status=job_status,
                    last_error=last_error,
                )
            )
        s.commit()
        return ws.id


def test_setup_status_running_polls_and_has_no_link(
    client_authed_owner, pg_session_factory, current_user
):
    wsid = _make_setup_workspace(pg_session_factory, current_user, job_status="running")
    r = client_authed_owner.get(f"/workspaces/{wsid}/setup-status")
    assert r.status_code == 200
    assert 'id="setup-status"' in r.text
    assert "hx-trigger" in r.text  # still polling
    assert "aria-busy" in r.text
    assert "/rubric-review" not in r.text


def test_setup_status_missing_job_falls_back_to_polling_card(
    client_authed_owner, pg_session_factory, current_user
):
    wsid = _make_setup_workspace(pg_session_factory, current_user, job_status=None)
    r = client_authed_owner.get(f"/workspaces/{wsid}/setup-status")
    assert r.status_code == 200
    assert "hx-trigger" in r.text
    assert "Setting up" in r.text


def test_setup_status_waiting_links_rubric_and_stops_polling(
    client_authed_owner, pg_session_factory, current_user
):
    wsid = _make_setup_workspace(
        pg_session_factory, current_user, job_status="waiting_for_rubric_review"
    )
    r = client_authed_owner.get(f"/workspaces/{wsid}/setup-status")
    assert r.status_code == 200
    assert f"/workspaces/{wsid}/rubric-review" in r.text
    assert "hx-trigger" not in r.text  # owner has an action: polling stops


def test_setup_status_failed_owner_sees_error_and_stops_polling(
    client_authed_owner, pg_session_factory, current_user
):
    wsid = _make_setup_workspace(
        pg_session_factory, current_user, job_status="failed", last_error="boom: no rubric"
    )
    r = client_authed_owner.get(f"/workspaces/{wsid}/setup-status")
    assert r.status_code == 200
    assert "Setup failed" in r.text
    assert "boom: no rubric" in r.text  # owner sees diagnostics
    assert "hx-trigger" not in r.text
    assert "/rubric-review" not in r.text


def test_setup_status_done_links_back_to_workspace(
    client_authed_owner, pg_session_factory, current_user
):
    # Cross-tab race: rubric confirmed elsewhere while this card still polls.
    wsid = _make_setup_workspace(pg_session_factory, current_user, job_status="done")
    r = client_authed_owner.get(f"/workspaces/{wsid}/setup-status")
    assert r.status_code == 200
    assert "Setup is complete" in r.text
    assert f'href="/workspaces/{wsid}"' in r.text
    assert "hx-trigger" not in r.text


def test_setup_status_viewer_waiting_keeps_polling_without_link(
    client_authed_user, pg_session_factory, current_user
):
    # P2: a viewer has no rubric action, so the waiting card must KEEP polling
    # to catch the owner's confirm (otherwise it's permanently stale).
    from call_manager.db.models import WorkspaceViewer
    from tests.proposal_assist.conftest import make_user

    with pg_session_factory() as s:
        owner = make_user(s, email="gate-owner@ki.se")
        s.flush()
        s.commit()
    wsid = _make_setup_workspace(pg_session_factory, owner, job_status="waiting_for_rubric_review")
    with pg_session_factory() as s:
        s.add(WorkspaceViewer(workspace_id=wsid, user_id=current_user.id))
        s.commit()
    r = client_authed_user.get(f"/workspaces/{wsid}/setup-status")
    assert r.status_code == 200
    assert "/rubric-review" not in r.text  # owner-only action
    assert "hx-trigger" in r.text  # still polling (P2)
    assert "Waiting for the owner" in r.text


def test_setup_status_failed_viewer_hidden_error(
    client_authed_user, pg_session_factory, current_user
):
    # P3b: raw worker exception text is owner-only; viewers get generic copy.
    from call_manager.db.models import WorkspaceViewer
    from tests.proposal_assist.conftest import make_user

    with pg_session_factory() as s:
        owner = make_user(s, email="gate-owner-f@ki.se")
        s.flush()
        s.commit()
    wsid = _make_setup_workspace(
        pg_session_factory, owner, job_status="failed", last_error="Traceback: secret path /srv/x"
    )
    with pg_session_factory() as s:
        s.add(WorkspaceViewer(workspace_id=wsid, user_id=current_user.id))
        s.commit()
    r = client_authed_user.get(f"/workspaces/{wsid}/setup-status")
    assert r.status_code == 200
    assert "Setup failed" in r.text
    assert "secret path" not in r.text  # raw diagnostics hidden from viewers


def test_setup_status_non_member_403_and_missing_404(
    client_authed_owner, pg_session_factory, current_user
):
    import uuid as _uuid

    from tests.proposal_assist.conftest import make_user

    with pg_session_factory() as s:
        other = make_user(s, email="gate-other@ki.se")
        s.flush()
        s.commit()
    wsid = _make_setup_workspace(pg_session_factory, other, job_status="running")
    r = client_authed_owner.get(f"/workspaces/{wsid}/setup-status")
    assert r.status_code == 403
    r = client_authed_owner.get(f"/workspaces/{_uuid.uuid4()}/setup-status")
    assert r.status_code == 404


def test_main_view_during_setup_shows_card_not_composer(
    client_authed_owner, pg_session_factory, current_user
):
    wsid = _make_setup_workspace(pg_session_factory, current_user, job_status="running")
    r = client_authed_owner.get(f"/workspaces/{wsid}")
    assert r.status_code == 200
    assert 'id="setup-status"' in r.text
    assert 'id="composer"' not in r.text
    # Exact chip markup (the card ALSO says "Setting up", so assert the chip).
    assert '<span class="phase-chip">Setting up</span>' in r.text


def test_main_view_waiting_shows_rubric_link(client_authed_owner, pg_session_factory, current_user):
    wsid = _make_setup_workspace(
        pg_session_factory, current_user, job_status="waiting_for_rubric_review"
    )
    r = client_authed_owner.get(f"/workspaces/{wsid}")
    assert r.status_code == 200
    assert f"/workspaces/{wsid}/rubric-review" in r.text
    assert 'id="composer"' not in r.text


def test_main_view_after_setup_shows_composer_not_card(
    client_authed_owner, pg_session_factory, current_user
):
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)  # spec phase
        s.commit()
        wsid = ws.id
    r = client_authed_owner.get(f"/workspaces/{wsid}")
    assert r.status_code == 200
    assert 'id="composer"' in r.text
    assert 'id="setup-status"' not in r.text
    assert '<span class="phase-chip">Drafting</span>' in r.text


def test_main_view_viewer_during_setup_sees_card_without_rubric_link(
    client_authed_user, pg_session_factory, current_user
):
    from call_manager.db.models import WorkspaceViewer
    from tests.proposal_assist.conftest import make_user

    with pg_session_factory() as s:
        owner = make_user(s, email="gate-owner2@ki.se")
        s.flush()
        s.commit()
    wsid = _make_setup_workspace(pg_session_factory, owner, job_status="waiting_for_rubric_review")
    with pg_session_factory() as s:
        s.add(WorkspaceViewer(workspace_id=wsid, user_id=current_user.id))
        s.commit()
    r = client_authed_user.get(f"/workspaces/{wsid}")
    assert r.status_code == 200
    assert 'id="setup-status"' in r.text
    assert "/rubric-review" not in r.text


# --------------------------------------------------------------------------
# Same-card parity: the main view's inline {% include setup_status.html %} and
# the GET /setup-status poll endpoint render the *same* card for a given state.
# Both paths build the card's context independently (workspaces.py
# get_workspace_main vs get_setup_status), so this guards against the two
# drifting — a drift would make the card visibly change the instant the first
# poll swaps in, mid-setup.
# --------------------------------------------------------------------------


def _extract_setup_card(html: str) -> str:
    """Pull the <div id="setup-status">…</div> fragment out of a page.

    The card contains no nested <div> elements (only <p>/<a>), so the first
    </div> after the opening tag closes it.
    """
    start = html.index('<div id="setup-status"')
    end = html.index("</div>", start) + len("</div>")
    return html[start:end]


def _normalize(fragment: str) -> str:
    """Collapse whitespace so indentation differences (the inline card is
    nested inside workspace_main.html; the polled card is top-level) don't
    mask a true content match — or hide a true content drift."""
    return " ".join(fragment.split())


@pytest.mark.parametrize(
    ("job_status", "last_error"),
    [
        ("running", None),
        ("waiting_for_rubric_review", None),
        ("failed", "boom: no rubric"),  # exercises setup_last_error parity
        ("done", None),
        (None, None),  # no job row → the "queued" fallback in both routes
    ],
)
def test_setup_card_inline_and_poll_endpoint_render_identically(
    client_authed_owner, pg_session_factory, current_user, job_status, last_error
):
    wsid = _make_setup_workspace(
        pg_session_factory, current_user, job_status=job_status, last_error=last_error
    )
    main = client_authed_owner.get(f"/workspaces/{wsid}")
    poll = client_authed_owner.get(f"/workspaces/{wsid}/setup-status")
    assert main.status_code == 200
    assert poll.status_code == 200

    inline_card = _extract_setup_card(main.text)
    poll_card = _extract_setup_card(poll.text)
    # Self-check the extraction grabbed a real card, not an empty slice.
    assert inline_card.startswith('<div id="setup-status"')
    assert inline_card.endswith("</div>")
    assert _normalize(inline_card) == _normalize(poll_card)


def test_setup_card_parity_for_viewer(client_authed_user, pg_session_factory, current_user):
    # is_owner=False parity: a viewer (current_user) on someone else's
    # workspace must see the identical card from both the main view and the
    # poll endpoint (no rubric link, still polling — P2).
    from call_manager.db.models import WorkspaceViewer
    from tests.proposal_assist.conftest import make_user

    with pg_session_factory() as s:
        owner = make_user(s, email="parity-owner@ki.se")
        s.flush()
        s.commit()
    wsid = _make_setup_workspace(pg_session_factory, owner, job_status="waiting_for_rubric_review")
    with pg_session_factory() as s:
        s.add(WorkspaceViewer(workspace_id=wsid, user_id=current_user.id))
        s.commit()

    main = client_authed_user.get(f"/workspaces/{wsid}")
    poll = client_authed_user.get(f"/workspaces/{wsid}/setup-status")
    assert main.status_code == 200
    assert poll.status_code == 200
    assert _normalize(_extract_setup_card(main.text)) == _normalize(_extract_setup_card(poll.text))


# --------------------------------------------------------------------------
# End-to-end closed loop: create → (Phase 0) → confirm rubric → composer live.
# Stitches the whole setup lifecycle through the real HTTP routes: the gate
# rejects turns and hides the composer while in 'setup'; confirming the rubric
# advances to 'spec'; the composer (and POST /turns) then go live.
# --------------------------------------------------------------------------


def test_create_confirm_composer_closed_loop(client_authed_owner, pg_session_factory, current_user):
    from sqlalchemy import select

    from call_manager.db.models import TurnJob, Workspace, WorkspaceSetupJob
    from tests.proposal_assist.conftest import make_workspace

    # Post-Phase-0 state: the setup worker has finished extraction and parked at
    # the rubric-review gate. (Phase 0 itself needs git + an LLM, so the test
    # constructs the state the worker leaves — same approach as the gate tests.)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=True, current_phase="setup")
        ws.rubric_criteria = [
            {"name": "Excellence", "description": "x", "weight": 1.0, "scale_max": 5},
        ]
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="waiting_for_rubric_review"))
        s.commit()
        wsid = ws.id

    # 1. Gate engaged: card shown, composer hidden, turns rejected.
    before = client_authed_owner.get(f"/workspaces/{wsid}")
    assert before.status_code == 200
    assert 'id="setup-status"' in before.text
    assert 'id="composer"' not in before.text
    early = client_authed_owner.post(f"/workspaces/{wsid}/turns", data={"content": "too early"})
    assert early.status_code == 409

    # 2. Confirm the rubric → advance to 'spec' and redirect to the main view.
    confirm = client_authed_owner.post(
        f"/workspaces/{wsid}/rubric-review",
        data={
            "criteria[0][name]": "Excellence",
            "criteria[0][description]": "x",
            "criteria[0][weight]": "1.0",
            "criteria[0][scale_max]": "5",
            "funder_notes": "",
        },
        follow_redirects=False,
    )
    assert confirm.status_code == 303
    assert confirm.headers["location"] == f"/workspaces/{wsid}"
    with pg_session_factory() as s:
        ws = s.get(Workspace, wsid)
        assert ws.current_phase == "spec"
        job = s.execute(
            select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == wsid)
        ).scalar_one()
        assert job.status == "done"

    # 3. Gate lifted: composer shown, card gone, turns accepted (TurnJob queued).
    after = client_authed_owner.get(f"/workspaces/{wsid}")
    assert after.status_code == 200
    assert 'id="composer"' in after.text
    assert 'id="setup-status"' not in after.text
    live = client_authed_owner.post(f"/workspaces/{wsid}/turns", data={"content": "hello"})
    assert live.status_code in (200, 201, 303)
    with pg_session_factory() as s:
        assert s.scalar(select(TurnJob).where(TurnJob.workspace_id == wsid)) is not None
