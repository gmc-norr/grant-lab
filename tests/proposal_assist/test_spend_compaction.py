"""Compaction spent real tokens off the books.

`ChatSummary.tokens` is `len(summary_text) // 4` — a character estimate of the
OUTPUT only, on a table no cap ever summed. And compaction never consulted the
cap, so it could spend past a ceiling that had already stopped every chat turn.
"""

import pytest
from sqlalchemy import select

from grant_lab.db.models import LlmSpend
from grant_lab.proposal_assist.spend import record_spend
from grant_lab.proposal_assist.turns.caps import GlobalTokenCapExceeded
from grant_lab.proposal_assist.turns.compaction import (
    compact_workspace,
)
from grant_lab.proposal_assist.turns.compaction import (
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
    from grant_lab.config import Settings

    def _run(*, tokens_in=0, tokens_out=0, workspace_id=None, outcome="ok"):
        from grant_lab.db.models import Workspace

        settings = Settings()
        with pg_session_factory() as s:
            if workspace_id is None:
                ws = make_workspace(s, owner=make_user(s, email="compact@umu.se"))
                workspace_id = ws.id
            else:
                ws = s.get(Workspace, workspace_id)
            for i in range(3):
                make_chat_turn(s, ws, role="user", content={"type": "text", "text": f"turn {i}"})
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
        from grant_lab.config import Settings

        settings = Settings()
        with pg_session_factory() as s:
            ws = make_workspace(s, owner=make_user(s, email="tick@umu.se"))
            for i in range(3):
                make_chat_turn(s, ws, role="user", content={"type": "text", "text": f"tick {i}"})
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
            ws = make_workspace(s, owner=make_user(s, email="capped@umu.se"))
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
                make_chat_turn(s, ws, role="user", content={"type": "text", "text": f"c{i}"})
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


def test_a_compaction_cut_off_after_the_api_answered_records_the_billed_usage(
    pg_session_factory, _settings_env
):
    """The REAL AnthropicClient: the reply hit max_tokens, so the client raises
    after Anthropic has billed the call. The failed row used to store 0 tokens,
    so the global cap never saw the spend and the tick re-billed it every 300 s
    (second review 2026-10-05). The error now carries the usage."""
    from unittest.mock import MagicMock, patch

    from grant_lab.config import Settings
    from grant_lab.ranking.anthropic_client import AnthropicClient
    from grant_lab.ranking.ollama_client import LLMError

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="cutoff@umu.se"))
        for i in range(3):
            make_chat_turn(s, ws, role="user", content={"type": "text", "text": f"t{i}"})
        s.commit()
        ws_id = ws.id
    client = AnthropicClient(api_key="sk-test", model="claude-opus-5")
    block = MagicMock(type="text", text="partial")
    reply = MagicMock(content=[block], model="claude-opus-5", stop_reason="max_tokens")
    reply.usage = MagicMock(
        input_tokens=900,
        output_tokens=4096,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    with (
        patch.object(client._client.messages, "create", return_value=reply),
        pytest.raises(LLMError),
    ):
        compact_workspace(
            session_factory=pg_session_factory,
            workspace_id=ws_id,
            summarizer_client=client,
            settings=Settings(),
        )
    with pg_session_factory() as s:
        row = s.execute(select(LlmSpend).where(LlmSpend.kind == "compaction")).scalar_one()
        assert (row.status, row.tokens_in, row.tokens_out) == ("failed", 900, 4096)


def _workspace_needing_compaction(s, email):
    ws = make_workspace(s, owner=make_user(s, email=email))
    turns = [
        make_chat_turn(s, ws, role="user", content={"type": "text", "text": f"{email} turn {i}"})
        for i in range(3)
    ]
    return ws, turns


def _spend_rows(s, ws_id, n, *, status, tokens_in, hours_ago, kind="compaction"):
    """n compaction-ledger rows, `hours_ago` old."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    for _ in range(n):
        record_spend(
            s,
            workspace_id=ws_id,
            kind=kind,
            status=status,
            tokens_in=tokens_in,
            tokens_out=0,
        )
    s.flush()
    s.execute(
        update(LlmSpend)
        .where(LlmSpend.workspace_id == ws_id, LlmSpend.kind == kind)
        .values(created_at=datetime.now(UTC) - timedelta(hours=hours_ago))
    )


_FAILED = dict(status="failed", tokens_in=900, hours_ago=1)


@pytest.mark.parametrize(
    "rows,summary_after,elsewhere,called",
    [
        # Three billed failures today: stop paying.
        (dict(_FAILED, n=3), False, False, False),
        # Two: one more try.
        (dict(_FAILED, n=2), False, False, True),
        # Older than 24 h: the window has passed.
        (dict(_FAILED, n=3, hours_ago=25), False, False, True),
        # Unbilled (the call never answered): retrying is free.
        (dict(_FAILED, n=3, tokens_in=0), False, False, True),
        # Timeouts: the server may have finished and billed (finding 1).
        (dict(_FAILED, n=3, status="timeout", tokens_in=0), False, False, False),
        # Billed "ok" rows with no summary after them: the call succeeded but the
        # summary write failed, every tick (finding 6).
        (dict(_FAILED, n=3, status="ok"), False, False, False),
        # A summary written after the failures: compaction works again, the old
        # failures no longer count (finding 2).
        (dict(_FAILED, n=3), True, False, True),
        # Three billed failures in ANOTHER workspace, or of another kind here:
        # none of this workspace's compaction business (finding 3).
        (dict(_FAILED, n=3), False, "workspace", True),
        (dict(_FAILED, n=3, kind="chat_turn"), False, False, True),
    ],
)
def test_the_tick_stops_retrying_a_compaction_that_keeps_costing(
    pg_session_factory, monkeypatch, _settings_env, rows, summary_after, elsewhere, called
):
    """A compaction that fails after the API answered (cut off, timed out, or
    the summary write failing) fails the same way on every 300 s tick, and
    each try is billed. After COMPACTION_MAX_BILLED_ATTEMPTS billed attempts
    since the last summary, within 24 h, the tick skips the workspace."""
    from unittest.mock import MagicMock

    from grant_lab.config import Settings
    from grant_lab.db.models import ChatSummary

    monkeypatch.setenv("COMPACTION_TURN_THRESHOLD", "2")
    with pg_session_factory() as s:
        ws, turns = _workspace_needing_compaction(s, "retry@umu.se")
        target = ws.id
        if elsewhere == "workspace":
            other, _ = _workspace_needing_compaction(s, "other@umu.se")
            target = other.id
        _spend_rows(s, target, **rows)
        if summary_after:
            s.add(
                ChatSummary(
                    workspace_id=ws.id,
                    summary="earlier",
                    summarized_through=turns[0].id,
                    tokens=1,
                    model="claude-opus-5",
                )
            )
        s.commit()
        ws_id = ws.id
    client = MagicMock()
    client.model = "claude-opus-5"
    client.chat.return_value = {
        "choices": [{"message": {"role": "assistant", "content": "A summary."}}],
        "model": "claude-opus-5",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    _run_compaction_tick(
        session_factory=pg_session_factory,
        summarizer_client_factory=lambda: client,
        settings=Settings(),
    )
    contents = [str(c.kwargs["messages"]) for c in client.chat.call_args_list]
    assert any("retry@umu.se turn 1" in m for m in contents) is called, ws_id


def test_a_timed_out_compaction_is_recorded_as_a_timeout(pg_session_factory, _settings_env):
    """The real client's timeout reaches compact_workspace as a TimeoutError and
    is recorded `timeout`, which the tick counts as possibly billed."""
    from unittest.mock import patch

    import anthropic
    import httpx

    from grant_lab.config import Settings
    from grant_lab.ranking.anthropic_client import AnthropicClient
    from grant_lab.ranking.ollama_client import LLMError

    with pg_session_factory() as s:
        ws, _ = _workspace_needing_compaction(s, "timeout@umu.se")
        s.commit()
        ws_id = ws.id
    client = AnthropicClient(api_key="sk-test", model="claude-opus-5", max_retries=0)
    timeout = anthropic.APITimeoutError(request=httpx.Request("POST", "https://api.example"))
    with (
        patch.object(client._client.messages, "create", side_effect=timeout),
        pytest.raises(LLMError),
    ):
        compact_workspace(
            session_factory=pg_session_factory,
            workspace_id=ws_id,
            summarizer_client=client,
            settings=Settings(),
        )
    with pg_session_factory() as s:
        row = s.execute(select(LlmSpend).where(LlmSpend.kind == "compaction")).scalar_one()
        assert row.status == "timeout"


def test_the_scheduled_compaction_client_does_not_retry_on_its_own(monkeypatch):
    """The tick is the retry. SDK retries on top made each billed attempt
    three (review 2026-10-05, finding 1). Built through the real
    build_scheduler, as test_kill_switch does."""
    from unittest.mock import MagicMock

    from grant_lab.config import Settings
    from grant_lab.proposal_assist.drafting.factory import build_workspace_controller
    from grant_lab.scheduler import build_scheduler

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@127.0.0.1/z")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("DRAFTING_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("DRAFTING_ADAPTER", "fake")
    settings = Settings()
    calls: list = []
    monkeypatch.setattr(
        "grant_lab.proposal_assist.turns.compaction.run_compaction_tick",
        lambda **kw: calls.append(kw),
    )

    def session_factory():
        return None

    sched = build_scheduler(
        session_factory=session_factory,
        settings=settings,
        alerter=MagicMock(),
        ollama_url=settings.ollama_url,
        gemma_model=settings.gemma_model,
        ranking_tick_seconds=settings.ranking_tick_seconds,
        ranking_queue_batch=settings.ranking_queue_batch,
        embedding_pregate_threshold=settings.embedding_pregate_threshold,
        embed_client=MagicMock(model="bge-m3"),
        workspace_controller=build_workspace_controller(
            settings=settings, session_factory=session_factory
        ),
        start=False,
    )
    try:
        sched.get_job("compaction_tick").func()
    finally:
        sched.shutdown(wait=False)
    (kw,) = calls
    assert kw["summarizer_client_factory"]()._client.max_retries == 0
