"""REMOTE_RANK_EFFORT reaches the remote ranker's client (review 2026-10-06 M1)
and stays out of the scoring fingerprint, so changing it re-ranks nothing."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from grant_lab.ranking.freshness import ScoringConfig


def _build(monkeypatch, effort: str | None):
    from grant_lab import scheduler as scheduler_mod
    from grant_lab.config import Settings
    from grant_lab.scheduler import build_scheduler

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@127.0.0.1/z")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("DRAFTING_ENABLED", "false")
    if effort is not None:
        monkeypatch.setenv("REMOTE_RANK_EFFORT", effort)
    settings = Settings().model_copy(
        update={
            "ranking_tick_seconds": 60,
            "ranking_queue_batch": 10,
            "embedding_pregate_threshold": 0.3,
            "embedding_hard_fail_threshold": None,
            "anthropic_api_key": "sk-test-not-real",
            "remote_rank_model": "claude-sonnet-5-5",
        }
    )
    built: list[dict] = []
    real = scheduler_mod.AnthropicClient

    def spy(**kw):
        built.append(kw)
        return real(**kw)

    monkeypatch.setattr(scheduler_mod, "AnthropicClient", spy)
    sched = build_scheduler(
        session_factory=lambda: None,
        settings=settings,
        alerter=MagicMock(),
        embed_client=MagicMock(model="bge-m3"),
        start=False,
    )
    sched.shutdown(wait=False)
    (remote,) = [kw for kw in built if kw.get("model") == "claude-sonnet-5-5"]
    return remote


@pytest.mark.parametrize(("env", "expected"), [(None, "high"), ("medium", "medium")])
def test_the_remote_ranker_is_built_with_the_configured_effort(monkeypatch, env, expected):
    assert _build(monkeypatch, env)["effort"] == expected


def test_effort_is_not_part_of_the_scoring_fingerprint():
    """Like the remote model id (spec D3, rev 3): a fingerprint change would
    re-rank every stored pair, Sonnet escalations included."""
    assert "effort" not in ScoringConfig.__dataclass_fields__


def test_the_remote_ranker_is_built_without_sdk_retries(monkeypatch):
    """Review 2026-10-26: the remote ranker still took the SDK's default
    `max_retries=2`, so ONE read timeout on a Sonnet escalation was up to three
    billed requests behind a single `ranking_remote_attempts` row — the drain
    tick is the retry (every RANKING_TICK_SECONDS, bounded by
    RANKING_REMOTE_DAILY_CAP), exactly as compaction, Phase 0 and the citation
    client already pin. `test_max_retries_reaches_the_sdk`
    (tests/test_ranking_anthropic_client.py) closes the other half of the chain:
    this kwarg becomes `anthropic.Anthropic(max_retries=...)`."""
    assert _build(monkeypatch, None)["max_retries"] == 0
