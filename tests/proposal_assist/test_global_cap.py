"""Global daily cap: when ALL workspaces' today usage sums above the
global ceiling, no new submit is accepted regardless of per-workspace
remaining budget. The cap is a guardrail against runaway org-wide
spend; small overshoot from concurrent submits is acceptable."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from call_manager.config import Settings
from call_manager.db.models import (
    ChatTurn,
    CoherenceJob,
    EvaluatorJob,
    ReviewRun,
    UncitedClaimsJob,
)
from call_manager.proposal_assist.turns.caps import (
    GlobalTokenCapExceeded,
    check_global_daily_token_cap,
)
from call_manager.proposal_assist.turns.submit import submit_user_turn
from tests.proposal_assist.conftest import make_user, make_workspace


def test_check_global_cap_sums_across_workspaces(pg_session_factory, _settings_env, monkeypatch):
    """Two workspaces' assistant rows from today must both count
    toward the global sum returned by check_global_daily_token_cap."""
    monkeypatch.setenv("GLOBAL_DAILY_TOKEN_CAP_INPUT", "1000")
    monkeypatch.setenv("GLOBAL_DAILY_TOKEN_CAP_OUTPUT", "1000")
    settings = Settings()

    with pg_session_factory() as s:
        owner = make_user(s)
        s.commit()
        ws_a = make_workspace(s, owner=owner, title="A", ack_disclosure=True)
        ws_b = make_workspace(s, owner=owner, title="B", ack_disclosure=True)
        s.commit()
        s.add_all(
            [
                ChatTurn(
                    workspace_id=ws_a.id,
                    role="assistant",
                    content={"type": "text", "text": "x"},
                    tokens_in=400,
                    tokens_out=10,
                ),
                ChatTurn(
                    workspace_id=ws_b.id,
                    role="assistant",
                    content={"type": "text", "text": "y"},
                    tokens_in=300,
                    tokens_out=20,
                ),
            ]
        )
        s.commit()

    with pg_session_factory() as s:
        result = check_global_daily_token_cap(s, settings=settings)
    assert result.used_in == 700
    assert result.used_out == 30
    assert result.allowed is True


def test_submit_raises_global_cap_exceeded(pg_session_factory, _settings_env, monkeypatch):
    """When the global sum is already at/above the cap, a submit on
    a workspace with PLENTY of per-workspace headroom still fails."""
    monkeypatch.setenv("GLOBAL_DAILY_TOKEN_CAP_INPUT", "500")
    # Per-workspace stays at default (500_000) — way above what we'll seed.
    settings = Settings()

    with pg_session_factory() as s:
        owner = make_user(s)
        s.commit()
        ws_a = make_workspace(s, owner=owner, title="A", ack_disclosure=True)
        ws_b = make_workspace(s, owner=owner, title="B", ack_disclosure=True)
        s.commit()
        # Seed: ws_a already used 500 input today via a completed turn.
        s.add(
            ChatTurn(
                workspace_id=ws_a.id,
                role="assistant",
                content={"type": "text", "text": "x"},
                tokens_in=500,
                created_at=datetime.now(UTC) - timedelta(minutes=1),
            )
        )
        s.commit()
        wsid_b, owner_id = ws_b.id, owner.id

    # Submit on ws_b should fail with GlobalTokenCapExceeded even
    # though ws_b's per-workspace usage is 0.
    with pg_session_factory() as s, pytest.raises(GlobalTokenCapExceeded):
        submit_user_turn(
            session=s,
            workspace_id=wsid_b,
            content_text="hi",
            submitted_by_user_id=owner_id,
            settings=settings,
        )


def test_global_cap_counts_background_job_spend(
    pg_session_factory, _settings_env, monkeypatch
):
    """The org-wide backstop must see EVERY member-triggerable cloud consumer, not
    just chat turns. The four background queues — coherence, evaluator, mock-panel
    review, uncited-claims — each track tokens on their own table; the global cap
    UNIONs all four so a bg-job runaway can't slip under it."""
    monkeypatch.setenv("GLOBAL_DAILY_TOKEN_CAP_INPUT", "100000")
    monkeypatch.setenv("GLOBAL_DAILY_TOKEN_CAP_OUTPUT", "100000")
    settings = Settings()

    with pg_session_factory() as s:
        owner = make_user(s)
        s.commit()
        ws = make_workspace(s, owner=owner, ack_disclosure=True)
        s.commit()
        # No chat turns at all — only background-job spend.
        s.add(
            CoherenceJob(
                workspace_id=ws.id,
                status="completed",
                input_tokens=100,
                output_tokens=10,
            )
        )
        s.add(
            EvaluatorJob(
                workspace_id=ws.id,
                status="completed",
                input_tokens=200,
                output_tokens=20,
            )
        )
        s.add(
            ReviewRun(
                workspace_id=ws.id,
                status="done",
                draft_sha="a" * 64,
                input_tokens=300,
                output_tokens=30,
                estimated_dollars=Decimal("0.0010"),
            )
        )
        s.add(
            UncitedClaimsJob(
                workspace_id=ws.id,
                status="completed",
                input_tokens=400,
                output_tokens=40,
            )
        )
        s.commit()

    with pg_session_factory() as s:
        result = check_global_daily_token_cap(s, settings=settings)
    # Without the UNION this would be 0/0 (all spend invisible to the cap).
    assert result.used_in == 1000
    assert result.used_out == 100


