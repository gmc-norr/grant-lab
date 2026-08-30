"""The four fan-out endpoints spend against a cap they never consulted.

`check_global_daily_token_cap` had exactly two callers: turns/submit.py and the
read-only admin panel. Coherence, evaluator, review and uncited-claims all
CONTRIBUTED to the total and never CHECKED it.
"""

from __future__ import annotations

import pytest

from call_manager.proposal_assist.spend import record_spend
from tests.proposal_assist.conftest import make_workspace

ENDPOINTS = [
    ("coherence", "/api/workspaces/{ws}/coherence-runs"),
    ("evaluator", "/api/workspaces/{ws}/evaluator-runs"),
    ("review", "/api/workspaces/{ws}/review-runs"),
    ("uncited_claims", "/api/workspaces/{ws}/uncited-claims-runs"),
]


def _make_ws(_app, current_user, pg_session_factory, tmp_path):
    """Create a workspace with ack_disclosure=True; point settings at tmp_path.

    current_phase defaults to "active" in make_workspace — do NOT override it.
    A "setup" workspace causes gate_cloud_llm to raise 409, which would mask the
    cap result and make both at-cap and under-cap tests give the same answer.
    """
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=True)
        s.commit()
        return str(ws.id), ws.id


@pytest.fixture
def at_global_cap():
    """Return a callable that seeds a workspace OVER the global input-token cap.

    The cap on `_app.state.settings` is lowered to 100 directly (env-var
    patching is too late — `_app` is already constructed with Settings() read
    at startup), then 200 input tokens are recorded against the workspace.
    The cloud gate (disclosure_ack / phase) must pass first, so
    ack_disclosure=True is set.

    Returns the workspace-id string for the route path.
    """
    def _seed(_app, current_user, pg_session_factory, tmp_path):
        # Mutate the live settings object; the router reads it from
        # request.app.state.settings on every call, so this takes effect
        # immediately.
        _app.state.settings.global_daily_token_cap_input = 100
        _app.state.settings.global_daily_token_cap_output = 10_000_000
        ws_id_str, ws_uuid = _make_ws(_app, current_user, pg_session_factory, tmp_path)
        with pg_session_factory() as s:
            record_spend(
                s,
                workspace_id=ws_uuid,
                kind="chat_turn",
                status="ok",
                tokens_in=200,
                tokens_out=5,
            )
            s.commit()
        return ws_id_str

    return _seed


@pytest.fixture
def under_global_cap():
    """Return a callable that seeds a workspace well UNDER the global cap.

    The cap is left at the default (5 000 000). No spend is seeded, so the
    endpoint must succeed with a real 200 carrying id+status, not merely a
    non-429 refusal shape (403/409/503 would also satisfy != 429).
    """
    def _seed(_app, current_user, pg_session_factory, tmp_path):
        ws_id_str, _ = _make_ws(_app, current_user, pg_session_factory, tmp_path)
        return ws_id_str

    return _seed


@pytest.mark.parametrize(("name", "path"), ENDPOINTS)
def test_refused_when_the_global_cap_is_exceeded(
    name, path, client_authed_owner, _app, current_user, pg_session_factory, tmp_path, at_global_cap
):
    ws_id = at_global_cap(_app, current_user, pg_session_factory, tmp_path)
    r = client_authed_owner.post(path.format(ws=ws_id))
    assert r.status_code == 429, r.text
    assert "cap" in r.text.lower()


@pytest.mark.parametrize(("name", "path"), ENDPOINTS)
def test_allowed_when_under_the_cap(
    name, path, client_authed_owner, _app, current_user,
    pg_session_factory, tmp_path, under_global_cap,
):
    # The other side of the gate: a blanket refusal would pass the test above.
    #
    # `!= 429` is not that assertion. It is satisfied by 403 (no disclosure ack),
    # 409 (workspace still in setup), 503 (drafting disabled) and 404/403 from
    # require_member — every refusal shape except the one being tested. Assert
    # the success contract instead. None of the four POST decorators sets
    # `status_code` (web/coherence.py:44, web/evaluator.py:47, web/review.py:135,
    # web/uncited_claims.py:80), so the success status is 200, and all four
    # responses carry `id` and `status`.
    ws_id = under_global_cap(_app, current_user, pg_session_factory, tmp_path)
    r = client_authed_owner.post(path.format(ws=ws_id))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"]
    assert body["status"] in ("queued", "running")
