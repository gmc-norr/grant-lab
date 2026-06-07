"""Tests for the list_unsupported_claims tool (Slice 3 Task 7).

list_unsupported_claims surfaces factual/empirical sentences in proposal.md that
lack a citation marker, asking the audit LLM (via CitationLLMService) to judge
each uncited sentence and propose a literature search query. Exercises
list_unsupported_claims_impl directly (plain dict, no MCP envelope), plus a
registered-closure smoke test asserting the MCP envelope shape.

A cited sentence (one carrying a marker) is skipped before any LLM call, so the
stub never sees it. The proposal sentence is the user's OWN prose (first-party),
so it is sent verbatim — NOT wrapped in <untrusted-reference>.
"""

import subprocess
import uuid
from pathlib import Path

import pytest

from call_manager.config import Settings
from call_manager.proposal_assist.citations import CitationLLMResult
from call_manager.proposal_assist.tools.audit import list_unsupported_claims_impl
from call_manager.proposal_assist.tools.proposal import PROPOSAL_FILENAME
from call_manager.proposal_assist.tools.registry import ToolContext, _collect_tools
from tests.proposal_assist.conftest import make_workspace


def _init_repo(repo_dir: Path, initial_content: str) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "agent@test"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=repo_dir, check=True)
    (repo_dir / PROPOSAL_FILENAME).write_text(initial_content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo_dir, check=True)


def _build_ctx(pg_session_factory, tmp_path, initial_proposal, *, citation_service=None):
    settings = Settings()
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir, initial_proposal)
    ctx = ToolContext(
        workspace_id=wsid,
        repo_dir=repo_dir,
        session_factory=pg_session_factory,
        settings=settings,
        citation_service=citation_service,
    )
    return ctx, wsid


class _StubCitationService:
    """Records calls; returns a single preset CitationLLMResult."""

    def __init__(self, result: CitationLLMResult):
        self._result = result
        self.calls: list[dict] = []

    def complete_json(self, *, workspace_id, op, system, user) -> CitationLLMResult:
        self.calls.append({"workspace_id": workspace_id, "op": op, "system": system, "user": user})
        return self._result


class _RaisingCitationService:
    """complete_json raises every call; the impl must skip + never raise."""

    def __init__(self):
        self.calls: list[dict] = []

    def complete_json(self, *, workspace_id, op, system, user) -> CitationLLMResult:
        self.calls.append({"op": op, "user": user})
        raise RuntimeError("LLM unavailable")


def _claim_result(needs_citation, suggested_query="recurrence adjuvant therapy"):
    return CitationLLMResult(
        data={"needs_citation": needs_citation, "suggested_query": suggested_query},
        usage=None,
        model="stub-model",
        cap_exceeded=False,
        cap_scope=None,
    )


def _cap_result():
    return CitationLLMResult(
        data=None,
        usage=None,
        model=None,
        cap_exceeded=True,
        cap_scope="daily",
    )


# A heading, an UNCITED factual sentence, and a CITED sentence (with a real
# marker hand-written in canonical form). The cited sentence must never reach
# the stub; the uncited one should.
UNCITED = "Adjuvant therapy reduces recurrence by thirty percent"
CITED = "Secondary endpoints were not significant"
MARKER = "[@smith2021]<!-- ev:ev-aaaaaaaaaaaa -->"
PROPOSAL = f"# Proposal\n\n{UNCITED}. {CITED}{MARKER}.\n"


@pytest.mark.asyncio
async def test_cited_sentence_excluded(pg_session_factory, _settings_env, tmp_path):
    """A sentence carrying a marker is never sent to the LLM and not surfaced."""
    stub = _StubCitationService(_claim_result(True))
    ctx, _wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL, citation_service=stub)

    out = await list_unsupported_claims_impl(ctx)

    judged = [c["user"] for c in stub.calls]
    assert all(CITED not in u for u in judged), judged
    assert all(CITED not in c["sentence"] for c in out["candidates"]), out["candidates"]


@pytest.mark.asyncio
async def test_uncited_factual_sentence_surfaced(pg_session_factory, _settings_env, tmp_path):
    """An uncited sentence the LLM flags needs_citation appears with query+location."""
    stub = _StubCitationService(_claim_result(True, suggested_query="adjuvant recurrence trial"))
    ctx, _wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL, citation_service=stub)

    out = await list_unsupported_claims_impl(ctx)

    assert out["semantic_available"] is True
    # The uncited sentence was sent to the LLM (first-party, NOT untrusted-wrapped).
    judged = [c["user"] for c in stub.calls]
    assert any(UNCITED in u for u in judged), judged
    assert all("<untrusted-reference>" not in u for u in judged), judged
    assert stub.calls[0]["op"] == "list_unsupported"

    matches = [c for c in out["candidates"] if UNCITED in c["sentence"]]
    assert len(matches) == 1, out["candidates"]
    cand = matches[0]
    assert cand["suggested_query"] == "adjuvant recurrence trial"
    assert cand["location"].startswith("char ")


@pytest.mark.asyncio
async def test_needs_citation_false_not_surfaced(pg_session_factory, _settings_env, tmp_path):
    """needs_citation False → the sentence is not a candidate."""
    stub = _StubCitationService(_claim_result(False))
    ctx, _wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL, citation_service=stub)

    out = await list_unsupported_claims_impl(ctx)

    assert out["semantic_available"] is True
    assert out["candidates"] == []
    # The uncited sentence was still judged (just not flagged).
    assert any(UNCITED in c["user"] for c in stub.calls), stub.calls


@pytest.mark.asyncio
async def test_service_none_returns_empty_unavailable(pg_session_factory, _settings_env, tmp_path):
    """citation_service=None → {candidates: [], semantic_available: False}."""
    ctx, _wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL, citation_service=None)

    out = await list_unsupported_claims_impl(ctx)

    assert out == {"candidates": [], "semantic_available": False}


@pytest.mark.asyncio
async def test_cap_exceeded_unavailable_partial(pg_session_factory, _settings_env, tmp_path):
    """cap_exceeded → stop, semantic_available False, no raise, partial/empty."""
    stub = _StubCitationService(_cap_result())
    ctx, _wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL, citation_service=stub)

    out = await list_unsupported_claims_impl(ctx)

    assert out["semantic_available"] is False
    assert out["candidates"] == []


@pytest.mark.asyncio
async def test_service_raises_skips_no_crash(pg_session_factory, _settings_env, tmp_path):
    """An exception per call is swallowed (sentence skipped), tool never raises."""
    stub = _RaisingCitationService()
    ctx, _wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL, citation_service=stub)

    out = await list_unsupported_claims_impl(ctx)

    assert out["semantic_available"] is True
    assert out["candidates"] == []
    assert len(stub.calls) >= 1


@pytest.mark.asyncio
async def test_missing_proposal_returns_error(pg_session_factory, _settings_env, tmp_path):
    stub = _StubCitationService(_claim_result(True))
    ctx, _wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL, citation_service=stub)
    (ctx.repo_dir / PROPOSAL_FILENAME).unlink()

    out = await list_unsupported_claims_impl(ctx)
    assert "error" in out


@pytest.mark.asyncio
async def test_registered_closure_smoke(pg_session_factory, _settings_env, tmp_path):
    """The registered list_unsupported_claims tool returns an MCP envelope and
    is present in _collect_tools."""
    stub = _StubCitationService(_claim_result(False))
    ctx, _wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL, citation_service=stub)

    tools = _collect_tools(ctx)
    names = {t.name for t in tools}
    assert "list_unsupported_claims" in names

    tool = next(t for t in tools if t.name == "list_unsupported_claims")
    result = await tool.handler({})
    assert "content" in result
    assert isinstance(result["content"], list)
    assert result["content"][0]["type"] == "text"
    assert "is_error" in result


def test_list_unsupported_claims_in_allowed_tools():
    from call_manager.proposal_assist.drafting.claude_adapter import ClaudeAgentSDKAdapter
    from call_manager.proposal_assist.drafting.contract import LaunchContext

    adapter = ClaudeAgentSDKAdapter(
        model="x",
        workspaces_dir="/tmp",
        tools_factory=lambda repo_dir, wsid: object(),  # type: ignore[arg-type]
    )
    opts = adapter._build_options_kwargs(LaunchContext(workspace_id=uuid.uuid4()))
    assert "list_unsupported_claims" in opts["allowed_tools"]
