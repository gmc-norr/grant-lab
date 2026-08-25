"""Tests for the read-only list_citations tool (citation provenance).

list_citations surfaces the citations.json ledger so the agent can discover the
`ev-...` evidence ids and the citekeys of what is already cited. Exercises the
*_impl directly (plain dict, no MCP envelope). Reuses the insert_citation
fixtures so the ledger rows under test are produced by the real insert path.
"""

import subprocess
import uuid
from pathlib import Path

import pytest

from call_manager.config import Settings
from call_manager.db.models import Reference, ReferenceChunk
from call_manager.proposal_assist.citations.core import insert_citation_impl
from call_manager.proposal_assist.citations.ledger import dump_ledger, empty_ledger
from call_manager.proposal_assist.ports.stamp import FenceStamp
from call_manager.proposal_assist.tools.citations import (
    CITATIONS_FILENAME,
    list_citations_impl,
)
from call_manager.proposal_assist.tools.proposal import PROPOSAL_FILENAME
from call_manager.proposal_assist.tools.registry import ToolContext
from tests.proposal_assist.conftest import make_workspace


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

EMBED_DIM = 1024


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


def _build_ctx(pg_session_factory, tmp_path, initial_proposal):
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
    )
    return ctx, wsid


CHUNK_TEXT = (
    "Adjuvant therapy reduces recurrence by 30% in the treated cohort. "
    "Secondary endpoints were not statistically significant."
)
ANCHOR_A = "recurrence is reduced substantially"
ANCHOR_B = "secondary endpoints fell short"
PROPOSAL = (
    f"# Proposal\n\nWe argue that {ANCHOR_A} in our population.\n\n"
    f"However, {ANCHOR_B} in the trial.\n"
)


@pytest.mark.asyncio
async def test_lists_inserted_citations(pg_session_factory, _settings_env, tmp_path):
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    rid = _seed_reference(
        pg_session_factory,
        wsid,
        chunks=[(0, "Results", CHUNK_TEXT)],
        csl_json={"author": [{"family": "Smith"}], "issued": {"date-parts": [[2021]]}},
    )

    r1 = await insert_citation_impl(
        ctx,
        stamp=_stamp(ctx),
        reference_id=str(rid),
        anchor=ANCHOR_A,
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
        stance="supports",
        confidence="high",
    )
    assert "error" not in r1, r1
    r2 = await insert_citation_impl(
        ctx,
        stamp=_stamp(ctx),
        reference_id=str(rid),
        anchor=ANCHOR_B,
        chunk_index=0,
        evidence_quote="Secondary endpoints were not statistically significant",
        stance="contradicts",
    )
    assert "error" not in r2, r2

    out = await list_citations_impl(ctx)
    citations = out["citations"]
    assert len(citations) == 2

    by_id = {c["ev_id"]: c for c in citations}
    assert set(by_id) == {r1["ev_id"], r2["ev_id"]}

    c1 = by_id[r1["ev_id"]]
    assert c1["citekey"] == "smith2021"
    assert c1["reference_id"] == str(rid)
    assert c1["chunk_index"] == 0
    assert c1["section_path"] == "Results"
    assert c1["stance"] == "supports"
    assert c1["confidence"] == "high"
    assert c1["evidence_quote"] == "reduces recurrence by 30%"
    assert ANCHOR_A in c1["claim"]
    assert c1["citation_snapshot"] == "Smith 2021"

    c2 = by_id[r2["ev_id"]]
    assert c2["stance"] == "contradicts"
    assert c2["evidence_quote"] == "Secondary endpoints were not statistically significant"

    # Order is document/ledger order (insert order).
    assert [c["ev_id"] for c in citations] == [r1["ev_id"], r2["ev_id"]]


@pytest.mark.asyncio
async def test_list_paginates_and_flags_truncation(pg_session_factory, _settings_env, tmp_path):
    """Defect B7-6: list_citations caps its output (default 100) and reports a
    `truncated` flag instead of projecting every row into one MCP block."""
    ctx, _wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    # 120 hand-built rows (cheaper than 120 real inserts; the cap is pure slicing).
    ledger = {
        **empty_ledger(),
        "evidence": [{"id": f"ev-{i:012x}", "citekey": f"k{i}"} for i in range(120)],
    }
    (ctx.repo_dir / CITATIONS_FILENAME).write_text(dump_ledger(ledger))

    # Default cap = 100 → truncated.
    out = await list_citations_impl(ctx)
    assert len(out["citations"]) == 100
    assert out["truncated"] is True

    # Explicit limit below the row count → still truncated, honoured.
    out = await list_citations_impl(ctx, limit=10)
    assert len(out["citations"]) == 10
    assert out["truncated"] is True

    # Limit above the row count → not truncated.
    out = await list_citations_impl(ctx, limit=500)
    assert len(out["citations"]) == 120
    assert out["truncated"] is False

    # Hard cap 500: a larger request is clamped (here rows < 500 so not truncated).
    out = await list_citations_impl(ctx, limit=10_000)
    assert len(out["citations"]) == 120
    assert out["truncated"] is False


@pytest.mark.asyncio
async def test_list_below_cap_not_truncated(pg_session_factory, _settings_env, tmp_path):
    """A workspace under the cap reports truncated=False."""
    ctx, _wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    ledger = {
        **empty_ledger(),
        "evidence": [{"id": f"ev-{i:012x}"} for i in range(3)],
    }
    (ctx.repo_dir / CITATIONS_FILENAME).write_text(dump_ledger(ledger))
    out = await list_citations_impl(ctx)
    assert len(out["citations"]) == 3
    assert out["truncated"] is False


@pytest.mark.asyncio
async def test_empty_workspace_returns_empty_list(pg_session_factory, _settings_env, tmp_path):
    ctx, _wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    # No citations.json written at all.
    assert await list_citations_impl(ctx) == {"citations": [], "truncated": False}


@pytest.mark.asyncio
async def test_malformed_rows_do_not_crash(pg_session_factory, _settings_env, tmp_path):
    """A hand-edited ledger row missing optional fields must not crash; a row
    missing its id is skipped."""
    ctx, _wsid = _build_ctx(pg_session_factory, tmp_path, PROPOSAL)
    ledger = {
        **empty_ledger(),
        "evidence": [
            # Sparse but valid: has id, everything else missing.
            {"id": "ev-sparse"},
            # No id at all → skipped.
            {"citekey": "ghost2000", "reference_id": str(uuid.uuid4())},
        ],
    }
    (ctx.repo_dir / CITATIONS_FILENAME).write_text(dump_ledger(ledger))

    out = await list_citations_impl(ctx)
    citations = out["citations"]
    assert len(citations) == 1
    row = citations[0]
    assert row["ev_id"] == "ev-sparse"
    assert row["citekey"] is None
    assert row["chunk_index"] is None
    assert row["section_path"] is None
    assert row["claim"] is None
    assert row["evidence_quote"] is None
