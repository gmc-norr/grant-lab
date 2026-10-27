"""One writer, one reader, and the None-handling that the old sites got wrong.

Every previous site wrote `job.input_tokens = done.tokens_in or 0` on the happy
path only. The `or 0` is the interesting part: an SDK that reports None for a
call that still consumed input must record a row, not be skipped.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from grant_lab.db.models import LlmSpend, User, Workspace
from grant_lab.proposal_assist.spend import record_spend, sum_spend_today


@pytest.fixture
def settings(_settings_env):
    """A real Settings. `sum_spend_today` needs one because a `timeout` row is
    charged at its kind's max output tokens, which is configuration."""
    from grant_lab.config import Settings

    return Settings()


def _ws(session, email="spend@umu.se"):
    u = User(status="active", email=email)
    session.add(u)
    session.flush()
    ws = Workspace(title="W", owner_id=u.id, current_phase="active")
    session.add(ws)
    session.commit()
    return ws.id


def test_records_one_row(pg_session_factory):
    with pg_session_factory() as s:
        ws_id = _ws(s)
        record_spend(s, workspace_id=ws_id, kind="chat_turn", status="ok",
                     tokens_in=5, tokens_out=7, model="m", ref_id="t1")
        s.commit()
        row = s.execute(select(LlmSpend)).scalar_one()
        assert (row.kind, row.status, row.tokens_in, row.tokens_out) == ("chat_turn", "ok", 5, 7)
        assert row.ref_id == "t1"


@pytest.mark.parametrize("value", [None, 0])
def test_none_tokens_become_zero_and_STILL_record(pg_session_factory, value):
    # The whole point. A failed or token-less call must leave a row saying so;
    # "we spent nothing" and "we did not look" are different facts, and the old
    # code could not tell them apart because it wrote no row at all.
    with pg_session_factory() as s:
        ws_id = _ws(s)
        record_spend(s, workspace_id=ws_id, kind="evaluator", status="failed",
                     tokens_in=value, tokens_out=value)
        s.commit()
        row = s.execute(select(LlmSpend)).scalar_one()
        assert (row.tokens_in, row.tokens_out) == (0, 0)
        assert row.status == "failed"


@pytest.mark.parametrize(
    "ref", [uuid.UUID("00000000-0000-0000-0000-0000000000ab"), 42, "already-text", None]
)
def test_ref_id_accepts_every_primary_key_shape(pg_session_factory, ref):
    # Job tables key on uuid, chat_turns on int, compaction has no row at all.
    # One text column serves all three; the coercion lives here, once.
    with pg_session_factory() as s:
        ws_id = _ws(s)
        record_spend(s, workspace_id=ws_id, kind="coherence", status="ok", ref_id=ref)
        s.commit()
        row = s.execute(select(LlmSpend)).scalar_one()
        assert row.ref_id == (None if ref is None else str(ref))


def test_sum_today_totals_every_kind(pg_session_factory, settings):
    with pg_session_factory() as s:
        ws_id = _ws(s)
        for kind in ("chat_turn", "coherence", "evaluator", "review",
                     "uncited_claims", "compaction", "citation"):
            record_spend(s, workspace_id=ws_id, kind=kind, status="ok",
                         tokens_in=1, tokens_out=2)
        s.commit()
        assert sum_spend_today(s, settings=settings) == (7, 14)


def test_sum_today_ignores_yesterday(pg_session_factory, settings):
    # The boundary is Stockholm midnight, from timeutil — NOT a fourth copy.
    from grant_lab.timeutil import stockholm_day_start_utc

    with pg_session_factory() as s:
        ws_id = _ws(s)
        record_spend(s, workspace_id=ws_id, kind="chat_turn", status="ok",
                     tokens_in=100, tokens_out=100)
        s.commit()
        row = s.execute(select(LlmSpend)).scalar_one()
        row.created_at = stockholm_day_start_utc() - timedelta(seconds=1)
        s.commit()
        assert sum_spend_today(s, settings=settings) == (0, 0)


def test_sum_today_can_scope_to_one_workspace(pg_session_factory, settings):
    with pg_session_factory() as s:
        a = _ws(s, "a@umu.se")
        b = _ws(s, "b@umu.se")
        record_spend(s, workspace_id=a, kind="chat_turn", status="ok", tokens_in=3, tokens_out=0)
        record_spend(s, workspace_id=b, kind="chat_turn", status="ok", tokens_in=9, tokens_out=0)
        s.commit()
        assert sum_spend_today(s, settings=settings, workspace_id=a) == (3, 0)
        assert sum_spend_today(s, settings=settings) == (12, 0)


def test_sum_today_takes_an_instant_so_it_can_be_tested(pg_session_factory, settings):
    # Same rule as timeutil: a function that reads the clock itself cannot be
    # falsified. There were three such day-boundary helpers and zero tests.
    with pg_session_factory() as s:
        ws_id = _ws(s)
        record_spend(s, workspace_id=ws_id, kind="chat_turn", status="ok",
                     tokens_in=4, tokens_out=0)
        s.commit()
        future = datetime.now(UTC) + timedelta(days=2)
        assert sum_spend_today(s, settings=settings, now=future) == (0, 0)


def test_sum_today_charges_a_timeout_at_that_kinds_max_output_tokens(pg_session_factory, settings):
    """Review 2026-10-26: the GLOBAL hosted cap ignored timeouts.

    A `timeout` row means a read timeout on a request that WAS SENT. The server
    may have finished and billed the reply, but the row carries 0 tokens because
    the client never saw a count. `citations/usage.py::_sum_today` has charged
    such a row at the client's own max_tokens since review 2026-10-06 M7 — this
    reader did not, so the SAME citation timeout row was money to one cap and
    free to the other, and a repeating timeout could spend forever under the
    global ceiling while the per-op ceiling held.

    `count_billed_attempts` already treats a `timeout` as possibly billed; the
    global sum was the one reader that did not."""
    with pg_session_factory() as s:
        ws_id = _ws(s)
        record_spend(s, workspace_id=ws_id, kind="citation", status="timeout",
                     tokens_in=0, tokens_out=0, model="claude-sonnet-5-5")
        s.commit()
        assert sum_spend_today(s, settings=settings) == (
            0,
            settings.citation_llm_max_output_tokens,
        ), "the same expression citations/usage.py::_sum_today charges"


def test_an_sdk_kinds_timeout_is_not_invented_out_of_thin_air(pg_session_factory, settings):
    """The M7 rule is "the client's own max_tokens is the most this reply can
    have cost in output". `compaction` and `setup` go through
    `ranking.anthropic_client.AnthropicClient`, whose max_tokens the scheduler's
    two factories leave at the class default, so that default IS the bound. The
    five Claude Agent SDK kinds have no max_tokens at all — an agent turn is a
    multi-call session — so there is no ceiling to charge, and inventing one
    would bill the cap for a number no client ever enforced. Pinned so the two
    halves cannot be confused for each other."""
    import inspect

    from grant_lab.proposal_assist.spend import ANTHROPIC_CLIENT_MAX_OUTPUT_TOKENS
    from grant_lab.ranking.anthropic_client import AnthropicClient

    assert (
        inspect.signature(AnthropicClient.__init__).parameters["max_tokens"].default
        == ANTHROPIC_CLIENT_MAX_OUTPUT_TOKENS
    ), "the charge must track the client it describes"

    with pg_session_factory() as s:
        ws_id = _ws(s)
        record_spend(s, workspace_id=ws_id, kind="compaction", status="timeout")
        record_spend(s, workspace_id=ws_id, kind="setup", status="timeout")
        # No client-side output ceiling exists for these, so they charge nothing.
        for kind in ("chat_turn", "coherence", "evaluator", "review", "uncited_claims"):
            record_spend(s, workspace_id=ws_id, kind=kind, status="timeout")
        s.commit()
        assert sum_spend_today(s, settings=settings) == (
            0,
            2 * ANTHROPIC_CLIENT_MAX_OUTPUT_TOKENS,
        )


def test_only_timeouts_get_the_charge(pg_session_factory, settings):
    """Non-vacuity on the status filter: an `ok` or `failed` row that really did
    cost nothing must not be charged a max-output reply. `failed` with no tokens
    never got an answer (spend.py's own rule); only `timeout` means "the server
    may have finished and billed it"."""
    with pg_session_factory() as s:
        ws_id = _ws(s)
        record_spend(s, workspace_id=ws_id, kind="citation", status="ok")
        record_spend(s, workspace_id=ws_id, kind="citation", status="failed")
        s.commit()
        assert sum_spend_today(s, settings=settings) == (0, 0)
