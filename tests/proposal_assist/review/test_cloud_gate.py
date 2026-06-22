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
    assert "running a mock-panel review" in ei.value.detail


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
