"""Tests for the validate_citations tool (Slice 3 Task 5).

validate_citations wires the deterministic + semantic audit layers behind a
read-only MCP tool. Exercises validate_citations_impl directly (plain dict, no
MCP envelope), plus a registered-closure smoke test that asserts the MCP
envelope shape. Reuses the insert_citation fixtures so the ledger rows under
test are produced by the real insert path.
"""

import subprocess
import uuid
from pathlib import Path

import pytest

from call_manager.config import Settings
from call_manager.db.models import Reference, ReferenceChunk
from call_manager.proposal_assist.citations import CitationLLMResult
from call_manager.proposal_assist.citations.core import insert_citation_impl
from call_manager.proposal_assist.ports.stamp import FenceStamp
from call_manager.proposal_assist.tools.audit import validate_citations_impl
from call_manager.proposal_assist.tools.proposal import PROPOSAL_FILENAME
from call_manager.proposal_assist.tools.registry import ToolContext, _collect_tools
from tests.proposal_assist.conftest import make_workspace

EMBED_DIM = 1024


def _stamp(ctx: ToolContext) -> FenceStamp:
    """The fencing identity insert_citation_impl now requires. These test
    workspaces are all at lifecycle_generation 0, so the stamp the caller would
    thread carries generation 0."""
    return FenceStamp(
        workspace_id=ctx.workspace_id,
        generation=0,
        active_turn_job_id=None,
        origin_job_id=None,
    )


def _init_repo(repo_dir: Path, initial_content: str) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "agent@test"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=repo_dir, check=True)
    (repo_dir / PROPOSAL_FILENAME).write_text(initial_content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo_dir, check=True)


def _seed_reference(
    pg_session_factory,
    workspace_id,
    *,
    chunks: list[tuple[int, str | None, str]],
    citekey: str | None = None,
    csl_json: dict | None = None,
) -> uuid.UUID:
    with pg_session_factory() as s:
        ref = Reference(
            workspace_id=workspace_id,
            kind="paper",
            source_type="uploaded",
            filename="seed.pdf",
            storage_path="seed/seed.pdf",
            mime_type="application/pdf",
            citekey=citekey,
            csl_json=csl_json,
            metadata_status="approved",
        )
        s.add(ref)
        s.flush()
        rid = ref.id
        for idx, section_path, text in chunks:
            s.add(
                ReferenceChunk(
                    reference_id=rid,
                    workspace_id=workspace_id,
                    chunk_index=idx,
                    section_path=section_path,
                    text=text,
                    embedding=[0.0] * EMBED_DIM,
                    embedding_model="fake-embedder",
                )
            )
        s.commit()
    return rid


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


def _verdict_result(verdict, rationale="because reasons"):
    return CitationLLMResult(
        data={"verdict": verdict, "rationale": rationale},
        usage=None,
        model="stub-model",
        cap_exceeded=False,
        cap_scope=None,
    )


CHUNK_TEXT = (
    "Adjuvant therapy reduces recurrence by 30% in the treated cohort. "
    "Secondary endpoints were not statistically significant."
)
ANCHOR = "recurrence is reduced substantially"
PROPOSAL = f"# Proposal\n\nWe argue that {ANCHOR} in our population.\n"


async def _insert_one(ctx, rid):
    res = await insert_citation_impl(
        ctx,
        stamp=_stamp(ctx),
        reference_id=str(rid),
        anchor=ANCHOR,
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
        stance="supports",
        confidence="high",
    )
    assert "error" not in res, res
    return res


@pytest.mark.asyncio
async def test_clean_deterministic_only_no_service(pg_session_factory, _settings_env, tmp_path):
    """citation_service=None → semantic_available False, no error-severity issues
    for a clean citation, checked >= 1."""
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL, citation_service=None)
    rid = _seed_reference(
        pg_session_factory,
        wsid,
        chunks=[(0, "Results", CHUNK_TEXT)],
        csl_json={"author": [{"family": "Smith"}], "issued": {"date-parts": [[2021]]}},
    )
    await _insert_one(ctx, rid)

    out = await validate_citations_impl(ctx)
    assert out["semantic_available"] is False
    assert out["checked"] >= 1
    errors = [i for i in out["issues"] if i["severity"] == "error"]
    assert errors == [], errors


@pytest.mark.asyncio
async def test_tampered_chunk_index_drift(pg_session_factory, _settings_env, tmp_path):
    """Mutating chunk.text in the DB after inserting yields an index_drift issue."""
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL, citation_service=None)
    rid = _seed_reference(
        pg_session_factory,
        wsid,
        chunks=[(0, "Results", CHUNK_TEXT)],
        csl_json={"author": [{"family": "Smith"}], "issued": {"date-parts": [[2021]]}},
    )
    await _insert_one(ctx, rid)

    # Tamper with the chunk text so its recomputed hash no longer matches.
    with pg_session_factory() as s:
        chunk = (
            s.query(ReferenceChunk)
            .filter(ReferenceChunk.reference_id == rid, ReferenceChunk.chunk_index == 0)
            .one()
        )
        chunk.text = CHUNK_TEXT + " Tampered tail sentence."
        s.commit()

    out = await validate_citations_impl(ctx)
    types = {i["type"] for i in out["issues"]}
    assert "index_drift" in types, out["issues"]


@pytest.mark.asyncio
async def test_semantic_contradicts_surfaced(pg_session_factory, _settings_env, tmp_path):
    """A stub service returning 'contradicts' surfaces a semantic_contradicts
    issue and semantic_available True."""
    stub = _StubCitationService(_verdict_result("contradicts"))
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL, citation_service=stub)
    rid = _seed_reference(
        pg_session_factory,
        wsid,
        chunks=[(0, "Results", CHUNK_TEXT)],
        csl_json={"author": [{"family": "Smith"}], "issued": {"date-parts": [[2021]]}},
    )
    await _insert_one(ctx, rid)

    out = await validate_citations_impl(ctx)
    assert out["semantic_available"] is True
    types = {i["type"] for i in out["issues"]}
    assert "semantic_contradicts" in types, out["issues"]
    assert len(stub.calls) == 1
    assert stub.calls[0]["op"] == "validate_citation"


@pytest.mark.asyncio
async def test_missing_proposal_returns_error(pg_session_factory, _settings_env, tmp_path):
    ctx, _wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL, citation_service=None)
    (ctx.repo_dir / PROPOSAL_FILENAME).unlink()
    out = await validate_citations_impl(ctx)
    assert "error" in out


@pytest.mark.asyncio
async def test_registered_closure_smoke(pg_session_factory, _settings_env, tmp_path):
    """The registered validate_citations tool returns an MCP envelope (not a
    raise) and is present in _collect_tools."""
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL, citation_service=None)
    rid = _seed_reference(pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)])
    await _insert_one(ctx, rid)

    tools = _collect_tools(ctx)
    names = {t.name for t in tools}
    assert "validate_citations" in names

    tool = next(t for t in tools if t.name == "validate_citations")
    result = await tool.handler({})
    assert "content" in result
    assert isinstance(result["content"], list)
    assert result["content"][0]["type"] == "text"
    assert "is_error" in result


def test_validate_citations_in_allowed_tools():
    from call_manager.proposal_assist.drafting.claude_adapter import ClaudeAgentSDKAdapter
    from call_manager.proposal_assist.drafting.contract import LaunchContext

    adapter = ClaudeAgentSDKAdapter(
        model="x",
        workspaces_dir="/tmp",
        tools_factory=lambda repo_dir, wsid: object(),  # type: ignore[arg-type]
    )
    opts = adapter._build_options_kwargs(LaunchContext(workspace_id=uuid.uuid4()))
    assert "mcp__proposal_assist__validate_citations" in opts["allowed_tools"]
