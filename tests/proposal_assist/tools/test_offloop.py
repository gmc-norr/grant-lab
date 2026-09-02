"""Review 2026-09-02 §2.7 / A11 F2: three agent-tool paths did blocking work ON
the event loop — the embedder round-trip in search_references, and one
synchronous LLM HTTP call per sentence / per ledger row in the audit tools with
no per-call cap (50 sentences with a 20 ms stub froze a 5 ms ticker for 1.01 s;
with local Gemma that is minutes per tool call, for every request, SSE stream,
collab socket and scheduler future on the single loop).

`asyncio.to_thread` (stdlib, so it passes the fence's import gate, as
tools/history.py already does) moves each of them off the loop; the unsupported-
claims judge is bounded per call; validate_citations goes through the same
capped validator the owner-facing route uses.
"""

from __future__ import annotations

import threading

import pytest

from call_manager.proposal_assist.citations import CitationLLMResult
from call_manager.proposal_assist.references.embed import EmbedResult
from call_manager.proposal_assist.tools import audit as audit_tools
from call_manager.proposal_assist.tools.audit import (
    list_unsupported_claims_impl,
    validate_citations_impl,
)
from call_manager.proposal_assist.tools.references import search_references_impl
from tests.proposal_assist.tools.test_list_unsupported_claims import _build_ctx


class _ThreadRecordingEmbedder:
    model = "fake-embedder"

    def __init__(self):
        self.threads: list[int] = []

    def embed(self, text: str) -> EmbedResult:
        self.threads.append(threading.get_ident())
        return EmbedResult(vector=[0.0] * 1024, embedded_chars=len(text))


class _ThreadRecordingService:
    """complete_json records the thread it ran on and answers 'needs_citation'."""

    def __init__(self):
        self.threads: list[int] = []

    def complete_json(self, *, workspace_id, op, system, user) -> CitationLLMResult:
        self.threads.append(threading.get_ident())
        return CitationLLMResult(
            data={"needs_citation": True, "suggested_query": "q"},
            usage=None,
            model="stub",
            cap_exceeded=False,
        )


@pytest.mark.asyncio
async def test_search_references_embeds_off_the_event_loop(
    pg_session_factory, tmp_path, _settings_env
):
    ctx, _ = _build_ctx(pg_session_factory, tmp_path, "# P\n\nbody.\n")
    embedder = _ThreadRecordingEmbedder()
    loop_thread = threading.get_ident()
    await search_references_impl(ctx, query="anything", top_k=3, embedder=embedder)
    assert embedder.threads and all(t != loop_thread for t in embedder.threads), (
        "the embedder round-trip ran on the event loop"
    )


@pytest.mark.asyncio
async def test_unsupported_claims_judge_runs_off_the_loop_and_is_bounded(
    pg_session_factory, tmp_path, monkeypatch, _settings_env
):
    sentences = " ".join(f"Finding number {i} is established in the population." for i in range(30))
    svc = _ThreadRecordingService()
    ctx, _ = _build_ctx(pg_session_factory, tmp_path, f"# P\n\n{sentences}\n", citation_service=svc)
    monkeypatch.setattr(audit_tools, "_MAX_JUDGED_SENTENCES", 5)
    loop_thread = threading.get_ident()

    result = await list_unsupported_claims_impl(ctx)

    assert len(svc.threads) == 5, "one judge call per sentence, bounded per tool call"
    assert all(t != loop_thread for t in svc.threads), "a judge call ran on the event loop"
    assert result["semantic_available"] is True
    assert result["truncated"] is True and result["remaining"] == 25
    assert len(result["candidates"]) == 5


@pytest.mark.asyncio
async def test_unsupported_claims_reports_no_truncation_under_the_bound(
    pg_session_factory, tmp_path, _settings_env
):
    svc = _ThreadRecordingService()
    ctx, _ = _build_ctx(
        pg_session_factory,
        tmp_path,
        "# P\n\nOne claim here. Another claim here.\n",
        citation_service=svc,
    )
    result = await list_unsupported_claims_impl(ctx)
    assert result["truncated"] is False and result["remaining"] == 0
    assert len(svc.threads) == 2


@pytest.mark.asyncio
async def test_validate_citations_uses_the_capped_validator_off_the_loop(
    pg_session_factory, tmp_path, monkeypatch, _settings_env
):
    seen: dict = {}
    loop_thread = threading.get_ident()

    def _capped(**kwargs):
        seen["thread"] = threading.get_ident()
        seen["max_fresh_calls"] = kwargs.get("max_fresh_calls")
        return [], 0, 3

    def _uncapped(**kwargs):
        raise AssertionError("the tool must not call the uncapped validator")

    monkeypatch.setattr(audit_tools, "validate_citations_semantic_capped", _capped)
    monkeypatch.setattr(audit_tools, "validate_citations_semantic", _uncapped, raising=False)
    ctx, _ = _build_ctx(
        pg_session_factory, tmp_path, "# P\n\nbody.\n", citation_service=_ThreadRecordingService()
    )

    result = await validate_citations_impl(ctx)

    assert seen["thread"] != loop_thread, "the audit ran on the event loop"
    assert seen["max_fresh_calls"] == 25
    assert result["deferred"] == 3
