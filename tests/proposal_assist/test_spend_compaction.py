"""Compaction spent real tokens off the books.

`ChatSummary.tokens` is `len(summary_text) // 4` — a character estimate of the
OUTPUT only, on a table no cap ever summed. And compaction never consulted the
cap, so it could spend past a ceiling that had already stopped every chat turn.
"""

import pytest
from sqlalchemy import select

from call_manager.db.models import LlmSpend
from call_manager.proposal_assist.spend import record_spend
from call_manager.proposal_assist.turns.caps import GlobalTokenCapExceeded
from call_manager.proposal_assist.turns.compaction import (
    compact_workspace,
)
from call_manager.proposal_assist.turns.compaction import (
    run_compaction_tick as _run_compaction_tick,
)
from tests.proposal_assist.conftest import make_chat_turn, make_user, make_workspace


def _stub_summarizer_client(*, tokens_in: int, tokens_out: int, outcome: str):
    """A stub AnthropicClient-like whose chat() mirrors the real client's
    envelope shape (`{"choices": [...], "model": ..., "usage": {...}}`).

    With outcome="ok" it reports the requested usage; with outcome="failed"
    it raises RuntimeError (no envelope, so no usage to report)."""
    from unittest.mock import MagicMock

    client = MagicMock()
    client.model = "claude-haiku-4-5"
    if outcome == "failed":
        client.chat.side_effect = RuntimeError("summarizer exploded")
    else:
        client.chat.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "A summary."}}],
            "model": "claude-haiku-4-5",
            "usage": {
                "prompt_tokens": tokens_in,
                "completion_tokens": tokens_out,
            },
        }
    return client


@pytest.fixture
def run_compaction(pg_session_factory, monkeypatch, _settings_env):
    """Drive the REAL compact_workspace with a stub summarizer client.

    Seeds enough un-summarized ChatTurn rows to get past compact_workspace's
    `if not turns: return None` guard — otherwise every downstream assertion is
    vacuous. Returns the workspace id. `outcome="failed"` makes the stub raise,
    which the recipe records then re-raises.
    """
    from call_manager.config import Settings

    def _run(*, tokens_in=0, tokens_out=0, workspace_id=None, outcome="ok"):
        from call_manager.db.models import Workspace

        settings = Settings()
        with pg_session_factory() as s:
            if workspace_id is None:
                ws = make_workspace(s, owner=make_user(s, email="compact@ki.se"))
                workspace_id = ws.id
            else:
                ws = s.get(Workspace, workspace_id)
            for i in range(3):
                make_chat_turn(
                    s, ws, role="user", content={"type": "text", "text": f"turn {i}"}
                )
            s.commit()
        client = _stub_summarizer_client(
            tokens_in=tokens_in, tokens_out=tokens_out, outcome=outcome
        )
        compact_workspace(
            session_factory=pg_session_factory,
            workspace_id=workspace_id,
            summarizer_client=client,
            settings=settings,
        )
        return workspace_id

    return _run


@pytest.fixture
def run_compaction_tick(pg_session_factory, monkeypatch, _settings_env):
    """Drive the REAL run_compaction_tick over a workspace that needs
    compaction. Returns the int it returns."""
    monkeypatch.setenv("COMPACTION_TURN_THRESHOLD", "2")

    def _run():
        from call_manager.config import Settings

        settings = Settings()
        with pg_session_factory() as s:
            ws = make_workspace(s, owner=make_user(s, email="tick@ki.se"))
            for i in range(3):
                make_chat_turn(
                    s, ws, role="user", content={"type": "text", "text": f"tick {i}"}
                )
            s.commit()
        client = _stub_summarizer_client(tokens_in=10, tokens_out=5, outcome="ok")
        return _run_compaction_tick(
            session_factory=pg_session_factory,
            summarizer_client_factory=lambda: client,
            settings=settings,
        )

    return _run


@pytest.fixture
def at_cap(pg_session_factory, monkeypatch, _settings_env):
    """Seed enough today-spend to exceed the global input cap, and return a
    workspace id that itself needs compaction.

    At THIS commit check_global_daily_token_cap sums ChatTurn (+ four job
    tables); after Task 7 it sums LlmSpend only. This fixture must keep tripping
    the cap across that cutover, so it seeds BOTH a ChatTurn AND a matching
    LlmSpend row above a lowered GLOBAL_DAILY_TOKEN_CAP_INPUT — whichever table
    the cap reads, today's spend is already over budget. The directly-inserted
    ChatTurn produces no ledger row on its own, so without the record_spend seed
    this test would silently start passing-for-the-wrong-reason once Task 7 lands
    (cap sees an empty ledger). The returned workspace also gets enough
    un-summarized turns to pass compact_workspace's `if not turns` guard, so the
    cap check — not an empty-turns no-op — is what stops it.
    """
    monkeypatch.setenv("GLOBAL_DAILY_TOKEN_CAP_INPUT", "1000")
    monkeypatch.setenv("COMPACTION_TURN_THRESHOLD", "2")

    def _seed():
        with pg_session_factory() as s:
            ws = make_workspace(s, owner=make_user(s, email="capped@ki.se"))
            # One over-cap turn (2000 > 1000) — the org is already over budget
            # for today before compaction is even attempted. Seeded in BOTH
            # ledgers so the cap trips whether it reads ChatTurn (now) or
            # llm_spend (after Task 7).
            make_chat_turn(
                s,
                ws,
                role="assistant",
                content={"type": "text", "text": "big"},
                tokens_in=2000,
            )
            record_spend(
                s,
                workspace_id=ws.id,
                kind="chat_turn",
                status="ok",
                tokens_in=2000,
                tokens_out=0,
            )
            # Un-summarized turns so compact_workspace reaches the cap check
            # rather than returning None for "no turns to summarize".
            for i in range(3):
                make_chat_turn(
                    s, ws, role="user", content={"type": "text", "text": f"c{i}"}
                )
            s.commit()
            return ws.id

    return _seed


def test_compaction_records_its_real_usage(pg_session_factory, run_compaction):
    run_compaction(tokens_in=500, tokens_out=120)
    with pg_session_factory() as s:
        row = s.execute(select(LlmSpend).where(LlmSpend.kind == "compaction")).scalar_one()
        # The REPORTED usage, not a character estimate of the summary text.
        assert (row.tokens_in, row.tokens_out) == (500, 120)
        assert row.ref_id is None  # compaction has no job row


def test_compaction_refuses_to_run_over_the_cap(pg_session_factory, run_compaction, at_cap):
    # It used to spend past a ceiling that had already stopped every chat turn.
    #
    # This asserts a RAISE, not a None. `compact_workspace` already returns None
    # for "there were no turns to summarize" (compaction.py:144-145), so
    # `assert result is None` would be satisfied by a compaction that never even
    # looked at the cap — and `ChatSummary` has no `skipped_reason` attribute
    # (models.py:1100-1133), so an `or result.skipped_reason == ...` second
    # disjunct is unreachable. The cap skip needs an outcome of its own.
    ws_id = at_cap()
    with pytest.raises(GlobalTokenCapExceeded):
        run_compaction(tokens_in=500, tokens_out=120, workspace_id=ws_id)
    with pg_session_factory() as s:
        assert s.execute(select(LlmSpend).where(LlmSpend.kind == "compaction")).first() is None


def test_the_tick_stops_at_the_cap_without_raising(pg_session_factory, run_compaction_tick, at_cap):
    # run_compaction_tick's generic `except Exception` (compaction.py:227) would
    # otherwise turn every capped workspace into a logged stack trace. The cap is
    # org-wide, so once it trips there is nothing left to try.
    at_cap()
    assert run_compaction_tick() == 0
    with pg_session_factory() as s:
        assert s.execute(select(LlmSpend).where(LlmSpend.kind == "compaction")).first() is None


def test_a_failed_compaction_still_records(pg_session_factory, run_compaction):
    # The summarizer raised, so there is no envelope and no usage to report. The
    # row's EXISTENCE is the fact being recorded: "we called it and it threw" is
    # a different fact from "we never called it", and the old code could express
    # neither. Do not invent a token count here — inventing one is the
    # character-estimate mistake this task removes.
    with pytest.raises(RuntimeError):
        run_compaction(outcome="failed")
    with pg_session_factory() as s:
        row = s.execute(select(LlmSpend).where(LlmSpend.kind == "compaction")).scalar_one()
        assert row.status == "failed"
        assert (row.tokens_in, row.tokens_out) == (0, 0)
