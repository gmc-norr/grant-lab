"""Review 2026-10-06 M7. The hosted citation client kept the SDK's
max_retries=2, so one read timeout could be three billed requests, and the
service records a timeout as a zero-token row that check_citation_caps (which
sums tokens only) never saw. Compaction pinned its client to max_retries=0 for
exactly this reason.

Pinned here: the client does not retry on its own, and each `timeout` row is
charged to the citation cap as a reply of the client's own max_tokens ceiling.
"""

from __future__ import annotations

import pytest

from grant_lab.config import Settings
from grant_lab.proposal_assist.citations.llm import build_citation_llm
from grant_lab.proposal_assist.citations.usage import check_citation_caps, record_usage
from tests.proposal_assist.conftest import make_user, make_workspace


@pytest.fixture
def settings(_settings_env):
    s = Settings()
    object.__setattr__(s, "citation_llm_max_output_tokens", 1000)
    return s


def _ws(pg_session_factory, email="owner@umu.se"):
    with pg_session_factory() as s:
        ws_id = make_workspace(s, owner=make_user(s, email=email)).id
        s.commit()
    return ws_id


def _rows(pg_session_factory, ws_id, status, n, tokens_out=0):
    with pg_session_factory() as s:
        for _ in range(n):
            record_usage(s, workspace_id=ws_id, op="verify", status=status, tokens_out=tokens_out)
        s.commit()


def test_the_anthropic_citation_client_does_not_retry_on_its_own(settings):
    object.__setattr__(settings, "citation_llm_provider", "anthropic")
    object.__setattr__(settings, "anthropic_api_key", "sk-test-not-real")
    object.__setattr__(settings, "citation_llm_allow_cloud", True)

    llm = build_citation_llm(settings)

    assert llm._client._client.max_retries == 0


def test_timeouts_count_toward_the_workspace_output_cap(pg_session_factory, settings):
    ws_id = _ws(pg_session_factory)
    object.__setattr__(settings, "citation_llm_daily_token_cap_output", 2000)
    _rows(pg_session_factory, ws_id, "timeout", 1)
    with pg_session_factory() as s:
        assert check_citation_caps(s, ws_id, settings=settings).exceeded is False

    _rows(pg_session_factory, ws_id, "timeout", 1)
    with pg_session_factory() as s:
        cap = check_citation_caps(s, ws_id, settings=settings)

    # 2 timeouts x 1000-token ceiling = 2000 >= the cap.
    assert (cap.exceeded, cap.scope) == (True, "workspace")


def test_timeouts_add_to_real_tokens(pg_session_factory, settings):
    ws_id = _ws(pg_session_factory)
    object.__setattr__(settings, "citation_llm_daily_token_cap_output", 1500)
    _rows(pg_session_factory, ws_id, "ok", 1, tokens_out=500)
    _rows(pg_session_factory, ws_id, "timeout", 1)
    with pg_session_factory() as s:
        assert check_citation_caps(s, ws_id, settings=settings).exceeded is True


def test_timeouts_in_other_workspaces_count_toward_the_global_cap(pg_session_factory, settings):
    ws_a, ws_b = _ws(pg_session_factory), _ws(pg_session_factory, email="other@umu.se")
    with pg_session_factory() as s:
        before = check_citation_caps(s, ws_b, settings=settings)
    assert before.exceeded is False
    # Set the global output cap just above what is already there today (other
    # tests on this worker may have written rows), then add two timeouts in A.
    from sqlalchemy import func, select

    from grant_lab.db.models import CitationLlmUsage
    from grant_lab.timeutil import stockholm_day_start_utc

    with pg_session_factory() as s:
        out_today, timeouts_today = s.execute(
            select(
                func.coalesce(func.sum(CitationLlmUsage.tokens_out), 0),
                func.count().filter(CitationLlmUsage.status == "timeout"),
            ).where(CitationLlmUsage.created_at >= stockholm_day_start_utc())
        ).one()
    already = int(out_today) + int(timeouts_today) * 1000
    object.__setattr__(settings, "global_citation_llm_daily_token_cap_output", already + 2000)
    _rows(pg_session_factory, ws_a, "timeout", 2)

    with pg_session_factory() as s:
        cap = check_citation_caps(s, ws_b, settings=settings)

    assert (cap.exceeded, cap.scope) == (True, "global")


def test_a_failure_that_billed_nothing_is_not_charged(pg_session_factory, settings):
    """`failed` with 0 tokens never got a reply (Ollama timeouts land here too:
    httpx's TimeoutException is not the builtin TimeoutError). Only `timeout`
    means a sent request whose reply we gave up on."""
    ws_id = _ws(pg_session_factory)
    object.__setattr__(settings, "citation_llm_daily_token_cap_output", 1)
    _rows(pg_session_factory, ws_id, "failed", 5)
    with pg_session_factory() as s:
        assert check_citation_caps(s, ws_id, settings=settings).exceeded is False
