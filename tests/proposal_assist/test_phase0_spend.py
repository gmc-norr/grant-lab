"""Phase 0 bills Opus twice per new workspace (sections + rubric). Before review
2026-10-06 H2 it did so outside every cap and the ledger: the reviewer drove the
real tick with the global cap already tripped and got `hosted chat() calls
made=2; llm_spend rows before=1 after=1`.

Pinned here: the tick refuses to build a client while the global cap is
tripped (the job stays queued for tomorrow), every extractor call writes a
`setup` ledger row on every terminal path, and the scheduled client does not
add SDK retries on top of the job's own five attempts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from unittest.mock import MagicMock, Mock

import pytest
from sqlalchemy import select

from grant_lab.db.models import LlmSpend, WorkspaceSetupJob
from grant_lab.proposal_assist.extractors.sections import SECTION_NAMES
from grant_lab.proposal_assist.phases.setup import run_phase0_tick
from grant_lab.proposal_assist.spend import record_spend
from grant_lab.ranking.anthropic_client import AnthropicTimeoutError
from grant_lab.ranking.ollama_client import LLMError
from tests.proposal_assist.conftest import make_call, make_workspace


@dataclass
class _CapSettings:
    global_daily_token_cap_input: int = 1_000_000
    global_daily_token_cap_output: int = 1_000_000


def _envelope(content: str, tokens_in: int, tokens_out: int) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "model": "claude-opus-5",
        "usage": {"prompt_tokens": tokens_in, "completion_tokens": tokens_out},
    }


_SECTIONS = json.dumps({s: f"text for {s}" for s in SECTION_NAMES})
_RUBRIC = json.dumps(
    {
        "criteria": [
            {"name": "Excellence", "description": "Quality", "weight": 1.0, "scale_max": 5}
        ],
        "funder_notes": "",
    }
)


@pytest.fixture
def queued_job(pg_session_factory):
    with pg_session_factory() as s:
        call = make_call(s)
        s.commit()
        ws = make_workspace(s, call_id=call.id)
        job = WorkspaceSetupJob(workspace_id=ws.id, status="queued")
        s.add(job)
        s.commit()
        return ws.id, job.id


def _tick(pg_session_factory, tmp_path, client_factory, settings=None):
    run_phase0_tick(
        session_factory=pg_session_factory,
        client_factory=client_factory,
        extractor_version="sections-v1",
        rubric_extractor_version="rubric-v1",
        workspaces_dir=str(tmp_path / "repos"),
        settings=settings or _CapSettings(),
    )


def _setup_rows(pg_session_factory, ws_id):
    with pg_session_factory() as s:
        return [
            (r.status, r.tokens_in, r.tokens_out, r.model, r.ref_id)
            for r in s.scalars(
                select(LlmSpend)
                .where(LlmSpend.workspace_id == ws_id, LlmSpend.kind == "setup")
                .order_by(LlmSpend.id)
            )
        ]


def _job(pg_session_factory, job_id):
    with pg_session_factory() as s:
        job = s.get(WorkspaceSetupJob, job_id)
        return job.status, job.attempts


def test_a_tripped_global_cap_builds_no_client_and_leaves_the_job_queued(
    pg_session_factory, tmp_path, queued_job
):
    ws_id, job_id = queued_job
    with pg_session_factory() as s:
        record_spend(s, workspace_id=ws_id, kind="chat_turn", status="ok", tokens_in=5000)
        s.commit()
    built = []

    _tick(
        pg_session_factory,
        tmp_path,
        lambda: built.append(1) or Mock(model="m"),
        settings=_CapSettings(global_daily_token_cap_input=5000),
    )

    assert built == [], "no Opus client may be built while the global cap is tripped"
    assert _job(pg_session_factory, job_id) == ("queued", 0), (
        "a cap denial is not a failure: no attempt is charged and the job waits"
    )


def test_the_output_cap_also_stops_the_tick(pg_session_factory, tmp_path, queued_job):
    ws_id, job_id = queued_job
    with pg_session_factory() as s:
        record_spend(s, workspace_id=ws_id, kind="chat_turn", status="ok", tokens_out=10)
        s.commit()
    built = []

    _tick(
        pg_session_factory,
        tmp_path,
        lambda: built.append(1) or Mock(model="m"),
        settings=_CapSettings(global_daily_token_cap_output=10),
    )

    assert built == []
    assert _job(pg_session_factory, job_id) == ("queued", 0)


def test_both_extractor_calls_write_a_setup_ledger_row(pg_session_factory, tmp_path, queued_job):
    ws_id, job_id = queued_job
    client = Mock(model="claude-opus-5")
    client.chat.side_effect = [_envelope(_SECTIONS, 1200, 300), _envelope(_RUBRIC, 1500, 400)]

    _tick(pg_session_factory, tmp_path, lambda: client)

    assert _job(pg_session_factory, job_id)[0] == "waiting_for_rubric_review"
    assert _setup_rows(pg_session_factory, ws_id) == [
        ("ok", 1200, 300, "claude-opus-5", str(job_id)),
        ("ok", 1500, 400, "claude-opus-5", str(job_id)),
    ]


def test_the_ledger_rows_reach_the_global_cap(pg_session_factory, tmp_path, queued_job):
    """The point of the ledger: tomorrow's cap check sees today's setup spend."""
    from grant_lab.proposal_assist.turns.caps import check_global_daily_token_cap

    ws_id, _ = queued_job
    client = Mock(model="claude-opus-5")
    client.chat.side_effect = [_envelope(_SECTIONS, 1200, 300), _envelope(_RUBRIC, 1500, 400)]
    _tick(pg_session_factory, tmp_path, lambda: client)

    with pg_session_factory() as s:
        result = check_global_daily_token_cap(
            s, settings=_CapSettings(global_daily_token_cap_input=2700)
        )
    assert result.used_in >= 2700
    assert result.allowed is False


def test_a_billed_api_failure_is_recorded_with_its_usage(pg_session_factory, tmp_path, queued_job):
    ws_id, job_id = queued_job
    err = LLMError("anthropic response truncated at max_tokens=4096")
    err.usage = {"prompt_tokens": 900, "completion_tokens": 4096}
    client = Mock(model="claude-opus-5")
    client.chat.side_effect = err

    _tick(pg_session_factory, tmp_path, lambda: client)

    assert _setup_rows(pg_session_factory, ws_id) == [
        ("failed", 900, 4096, "claude-opus-5", str(job_id))
    ]
    assert _job(pg_session_factory, job_id) == ("queued", 1), "an LLM failure is retried"


def test_an_unbilled_failure_still_writes_a_row(pg_session_factory, tmp_path, queued_job):
    """'We spent nothing' and 'we never looked' are different facts (spend.py)."""
    ws_id, job_id = queued_job
    client = Mock(model="claude-opus-5")
    client.chat.side_effect = LLMError("anthropic connection error: refused")

    _tick(pg_session_factory, tmp_path, lambda: client)

    assert _setup_rows(pg_session_factory, ws_id) == [
        ("failed", 0, 0, "claude-opus-5", str(job_id))
    ]


def test_a_read_timeout_is_recorded_as_timeout(pg_session_factory, tmp_path, queued_job):
    ws_id, job_id = queued_job
    client = Mock(model="claude-opus-5")
    client.chat.side_effect = AnthropicTimeoutError("anthropic timeout")

    _tick(pg_session_factory, tmp_path, lambda: client)

    assert _setup_rows(pg_session_factory, ws_id) == [
        ("timeout", 0, 0, "claude-opus-5", str(job_id))
    ]


def test_a_reply_that_fails_to_parse_keeps_its_billed_tokens(
    pg_session_factory, tmp_path, queued_job
):
    """The API answered (and billed); the extractor then rejected the JSON."""
    ws_id, job_id = queued_job
    client = Mock(model="claude-opus-5")
    client.chat.side_effect = [_envelope("not json", 1100, 50)]

    _tick(pg_session_factory, tmp_path, lambda: client)

    assert _setup_rows(pg_session_factory, ws_id) == [
        ("failed", 1100, 50, "claude-opus-5", str(job_id))
    ]


def test_a_rubric_failure_after_good_sections_records_both(
    pg_session_factory, tmp_path, queued_job
):
    ws_id, job_id = queued_job
    client = Mock(model="claude-opus-5")
    client.chat.side_effect = [_envelope(_SECTIONS, 1200, 300), _envelope("[]", 1500, 10)]

    _tick(pg_session_factory, tmp_path, lambda: client)

    assert _setup_rows(pg_session_factory, ws_id) == [
        ("ok", 1200, 300, "claude-opus-5", str(job_id)),
        ("failed", 1500, 10, "claude-opus-5", str(job_id)),
    ]


def test_the_scheduled_phase0_client_does_not_retry_on_its_own(monkeypatch):
    """The job's five attempts are the retry. SDK retries on top made each
    attempt up to three billed Opus calls. Also pins that the scheduler hands
    the tick its settings, which is what the cap check reads."""
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
        "grant_lab.proposal_assist.phases.setup.run_phase0_tick",
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
        sched.get_job("proposal_assist_phase0_tick").func()
    finally:
        sched.shutdown(wait=False)
    (kw,) = calls
    assert kw["client_factory"]()._client.max_retries == 0
    assert kw["settings"] is settings


def test_cache_reads_and_writes_count_as_input(pg_session_factory, tmp_path, queued_job):
    """Anthropic reports cache tokens apart from prompt_tokens; the extractors'
    system prompt is cacheable, so prompt_tokens alone would undercount."""
    ws_id, job_id = queued_job
    sections = _envelope(_SECTIONS, 100, 300)
    sections["usage"].update(cache_creation_input_tokens=2000, cache_read_input_tokens=None)
    rubric = _envelope(_RUBRIC, 100, 400)
    rubric["usage"].update(cache_creation_input_tokens=None, cache_read_input_tokens=2000)
    client = Mock(model="claude-opus-5")
    client.chat.side_effect = [sections, rubric]

    _tick(pg_session_factory, tmp_path, lambda: client)

    assert [row[1] for row in _setup_rows(pg_session_factory, ws_id)] == [2100, 2100]


def test_the_ok_row_survives_a_persist_that_rolls_back(pg_session_factory, tmp_path, queued_job):
    """The call is re-ingested while Opus works, so the locked persist raises
    CallSourceChanged and rolls back. The money was spent anyway: the row must
    not roll back with it."""
    from grant_lab.db.models import Call, Workspace

    ws_id, job_id = queued_job

    def reingest_then_answer(**_kw):
        with pg_session_factory() as s:
            call = s.get(Call, s.get(Workspace, ws_id).call_id)
            call.full_text = (call.full_text or "") + " amended by a re-ingest"
            s.commit()
        return _envelope(_SECTIONS, 1200, 300)

    client = Mock(model="claude-opus-5")
    client.chat.side_effect = reingest_then_answer

    _tick(pg_session_factory, tmp_path, lambda: client)

    assert _setup_rows(pg_session_factory, ws_id) == [
        ("ok", 1200, 300, "claude-opus-5", str(job_id))
    ]
    with pg_session_factory() as s:
        assert "CallSourceChanged" in (s.get(WorkspaceSetupJob, job_id).last_error or "")
