"""The cap sums the ledger and nothing else.

It used to sum FIVE tables on THREE different timestamp columns. The per-job
columns still exist and are still written — they are the UI's display cache —
but they are no longer what the cap believes.
"""

import inspect
from dataclasses import dataclass

import pytest

from grant_lab.proposal_assist.spend import record_spend
from grant_lab.proposal_assist.turns.caps import check_global_daily_token_cap
from tests.proposal_assist.conftest import make_workspace


@dataclass
class _CapSettings:
    """Only the two fields check_global_daily_token_cap reads. A real Settings
    would work too, but this makes the caps under test explicit and avoids
    dragging the whole pydantic env in."""

    global_daily_token_cap_input: int
    global_daily_token_cap_output: int


@pytest.fixture
def settings_at_caps():
    def _make(*, cap_in: int = 1_000_000, cap_out: int = 1_000_000) -> _CapSettings:
        return _CapSettings(
            global_daily_token_cap_input=cap_in,
            global_daily_token_cap_output=cap_out,
        )

    return _make


@pytest.fixture
def seed_ws(pg_session_factory):
    """Create a workspace and return its id (a fresh owner each call)."""

    def _make():
        with pg_session_factory() as s:
            ws = make_workspace(s, ack_disclosure=True)
            s.commit()
            return ws.id

    return _make


def test_the_total_comes_from_the_ledger(pg_session_factory, settings_at_caps, seed_ws):
    ws_id = seed_ws()
    with pg_session_factory() as s:
        record_spend(s, workspace_id=ws_id, kind="coherence", status="ok",
                     tokens_in=1000, tokens_out=0)
        s.commit()
        result = check_global_daily_token_cap(s, settings=settings_at_caps(cap_in=1500))
        assert result.used_in == 1000
        assert result.allowed is True


def test_the_cap_trips_on_ledger_rows_alone(pg_session_factory, settings_at_caps, seed_ws):
    ws_id = seed_ws()
    with pg_session_factory() as s:
        record_spend(s, workspace_id=ws_id, kind="citation", status="ok",
                     tokens_in=2000, tokens_out=0)
        s.commit()
        result = check_global_daily_token_cap(s, settings=settings_at_caps(cap_in=1500))
        assert result.allowed is False
        assert result.reason == "input_cap_exceeded"


def test_a_FAILED_run_counts_toward_the_cap(pg_session_factory, settings_at_caps, seed_ws):
    # Failures cost money. The old code recorded none of them.
    ws_id = seed_ws()
    with pg_session_factory() as s:
        record_spend(s, workspace_id=ws_id, kind="evaluator", status="failed",
                     tokens_in=2000, tokens_out=0)
        s.commit()
        result = check_global_daily_token_cap(s, settings=settings_at_caps(cap_in=1500))
        assert result.allowed is False


def test_the_five_table_re_derivation_is_gone(pg_session_factory):
    # The mechanism, not just the number: five SELECTs on three timestamp
    # columns is what made every new feature a chance to forget one.
    src = inspect.getsource(check_global_daily_token_cap)
    for gone in ("ChatTurn.tokens_in", "CoherenceJob.input_tokens",
                 "EvaluatorJob.input_tokens", "ReviewRun.input_tokens",
                 "UncitedClaimsJob.input_tokens"):
        assert gone not in src, f"{gone} — the cap still re-derives from job tables"
    assert "sum_spend_today" in src


def test_stale_job_columns_no_longer_move_the_cap(pg_session_factory, settings_at_caps, seed_ws):
    # The columns are a display cache now. Writing a huge value into one and no
    # ledger row must NOT trip the cap — that is what "the ledger is the source
    # of truth" means, stated as a test rather than a comment.
    from grant_lab.db.models import CoherenceJob

    ws_id = seed_ws()
    with pg_session_factory() as s:
        job = CoherenceJob(workspace_id=ws_id, status="completed", input_tokens=999_999)
        s.add(job)
        s.commit()
        result = check_global_daily_token_cap(s, settings=settings_at_caps(cap_in=1500))
        assert result.allowed is True
