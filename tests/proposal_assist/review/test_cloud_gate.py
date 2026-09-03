from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from call_manager.proposal_assist.web._cloud_gate import gate_cloud_llm


def _req(drafting_enabled=True):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        settings=SimpleNamespace(drafting_enabled=drafting_enabled))))


def test_gate_403_message_uses_disclosure_action(monkeypatch):
    ws = SimpleNamespace(current_phase="active", id="W")
    user = SimpleNamespace(id=7)

    class _S:  # session whose scalar() returns None (no prior ack)
        def scalar(self, *a, **k):
            return None

    with pytest.raises(HTTPException) as ei:
        gate_cloud_llm(ws=ws, request=_req(), session=_S(), user=user,
                       disclosure_action="running a mock-panel review")
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "disclosure_required"
    assert "running a mock-panel review" in ei.value.detail["message"]


def test_gate_409_setup_and_503_drafting():
    user = SimpleNamespace(id=1)
    with pytest.raises(HTTPException) as ei:
        gate_cloud_llm(ws=SimpleNamespace(current_phase="setup", id="W"),
                       request=_req(), session=object(), user=user, disclosure_action="x")
    assert ei.value.status_code == 409
    with pytest.raises(HTTPException) as ei:
        gate_cloud_llm(ws=SimpleNamespace(current_phase="active", id="W"),
                       request=_req(drafting_enabled=False), session=object(), user=user,
                       disclosure_action="x")
    assert ei.value.status_code == 503


# --- The predicate the stub above can never reach ----------------------------
#
# The tests above pass a session whose `scalar()` returns None unconditionally,
# so the `select(...)` in `has_disclosure_ack` — including
# `WorkspaceEvent.actor_id == user_id` — is built and discarded. Measured:
# deleting that predicate left the ENTIRE suite green at 4084 passed.
#
# It is the only thing that matters here. The four cloud triggers are gated on
# `require_member`, NOT `require_owner` (deliberately — see the AUTHORITY
# comments in evaluator.py and uncited_claims.py), so per-actor acknowledgement
# is all that stands between a non-owner and hosted-model disclosure of the
# draft. Without it, viewer B's action ships the proposal to Anthropic on the
# OWNER's consent record.
#
# A real session is required to test this. That is the whole point.


def _real_req(drafting_enabled=True):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(settings=SimpleNamespace(drafting_enabled=drafting_enabled))
        )
    )


def test_one_members_ack_does_not_consent_for_another(pg_session_factory):
    from call_manager.db.models import WorkspaceViewer
    from tests.proposal_assist.conftest import make_user, make_workspace

    with pg_session_factory() as s:
        owner = make_user(s, email="ack-owner@umu.se")
        other = make_user(s, email="ack-other@umu.se")
        ws = make_workspace(s, owner=owner, ack_disclosure=True, current_phase="active")
        s.flush()
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=other.id))
        s.commit()

        # The owner acknowledged, so the owner passes.
        gate_cloud_llm(
            ws=ws, request=_real_req(), session=s, user=owner, disclosure_action="drafting"
        )

        # The other member did not. Same workspace, same event row, different actor.
        with pytest.raises(HTTPException) as ei:
            gate_cloud_llm(
                ws=ws, request=_real_req(), session=s, user=other, disclosure_action="drafting"
            )
        assert ei.value.status_code == 403
