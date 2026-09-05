"""Global daily cap: when ALL workspaces' today usage sums above the
global ceiling, no new submit is accepted regardless of per-workspace
remaining budget. The cap is a guardrail against runaway org-wide
spend; small overshoot from concurrent submits is acceptable."""

import pytest

from grant_lab.config import Settings
from grant_lab.proposal_assist.spend import record_spend
from grant_lab.proposal_assist.turns.caps import (
    GlobalTokenCapExceeded,
    check_global_daily_token_cap,
)
from grant_lab.proposal_assist.turns.submit import submit_user_turn
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
        # Source of truth is the ledger now, not the ChatTurn rows themselves —
        # seed both workspaces' spend there. Intent unchanged: the global sum
        # must span workspaces.
        record_spend(
            s, workspace_id=ws_a.id, kind="chat_turn", status="ok",
            tokens_in=400, tokens_out=10,
        )
        record_spend(
            s, workspace_id=ws_b.id, kind="chat_turn", status="ok",
            tokens_in=300, tokens_out=20,
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
        # Seed: ws_a already used 500 input today. The cap reads the ledger,
        # so record it there.
        record_spend(
            s, workspace_id=ws_a.id, kind="chat_turn", status="ok", tokens_in=500,
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
    review, uncited-claims — each record their tokens to the llm_spend ledger; the
    ledger is the single source of truth the global cap sums, so a bg-job runaway
    can't slip under it."""
    monkeypatch.setenv("GLOBAL_DAILY_TOKEN_CAP_INPUT", "100000")
    monkeypatch.setenv("GLOBAL_DAILY_TOKEN_CAP_OUTPUT", "100000")
    settings = Settings()

    with pg_session_factory() as s:
        owner = make_user(s)
        s.commit()
        ws = make_workspace(s, owner=owner, ack_disclosure=True)
        s.commit()
        # No chat turns at all — only background-job spend, recorded to the ledger.
        record_spend(
            s, workspace_id=ws.id, kind="coherence", status="ok",
            tokens_in=100, tokens_out=10,
        )
        record_spend(
            s, workspace_id=ws.id, kind="evaluator", status="ok",
            tokens_in=200, tokens_out=20,
        )
        record_spend(
            s, workspace_id=ws.id, kind="review", status="ok",
            tokens_in=300, tokens_out=30,
        )
        record_spend(
            s, workspace_id=ws.id, kind="uncited_claims", status="ok",
            tokens_in=400, tokens_out=40,
        )
        s.commit()

    with pg_session_factory() as s:
        result = check_global_daily_token_cap(s, settings=settings)
    # The ledger sums all four background consumers' spend.
    assert result.used_in == 1000
    assert result.used_out == 100


