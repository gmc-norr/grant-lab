"""propose_citation_impl: verifies the citation at PROPOSE time (reference,
chunk, quote, anchor) and writes a pending citation_insert proposed_change.
No git commit (unlike insert_citation) — propose writes only a DB row."""

import subprocess
import uuid
from pathlib import Path

import pytest

from call_manager.config import Settings
from call_manager.db.models import ProposedChange, Reference, ReferenceChunk
from call_manager.proposal_assist.tools.citations import propose_citation_impl
from call_manager.proposal_assist.tools.proposal import (
    PROPOSAL_FILENAME,
    read_proposal_impl,
)
from call_manager.proposal_assist.tools.registry import ToolContext
from tests.proposal_assist.conftest import make_workspace

EMBED_DIM = 1024
CHUNK_TEXT = (
    "Adjuvant therapy reduces recurrence by 30% in the treated cohort. "
    "Secondary endpoints were not statistically significant."
)
ANCHOR = "recurrence is reduced substantially"
PROPOSAL = "preamble\n\n## Methods\nWe argue that " + ANCHOR + " in our population.\n"


def _init_repo(repo_dir: Path, content: str) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo_dir, check=True)
    (repo_dir / PROPOSAL_FILENAME).write_text(content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_dir, check=True)


def _commit_count(repo_dir: Path) -> int:
    out = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    return int(out.stdout.strip())


def _seed_reference(pg_session_factory, workspace_id, *, chunks):
    with pg_session_factory() as s:
        ref = Reference(
            workspace_id=workspace_id,
            kind="paper",
            source_type="uploaded",
            filename="seed.pdf",
            storage_path="seed/seed.pdf",
            mime_type="application/pdf",
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


def _build_ctx(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id
    repo = Path(tmp_path) / str(wsid) / "repo"
    _init_repo(repo, PROPOSAL)
    ctx = ToolContext(
        workspace_id=wsid,
        repo_dir=repo,
        session_factory=pg_session_factory,
        settings=Settings(),
    )
    return ctx, wsid, repo


@pytest.mark.asyncio
async def test_propose_citation_writes_row_and_maps_section(
    pg_session_factory, _settings_env, tmp_path
):
    ctx, wsid, repo = _build_ctx(pg_session_factory, tmp_path)
    rid = _seed_reference(pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)])
    await read_proposal_impl(ctx)  # populate agent_view_head + sections
    before = _commit_count(repo)

    result = await propose_citation_impl(
        ctx,
        reference_id=str(rid),
        anchor=ANCHOR,
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
        stance="supports",
    )

    assert "error" not in result, result
    assert result["status"] == "pending"
    # No git commit — propose writes only a DB row.
    assert _commit_count(repo) == before

    with pg_session_factory() as s:
        pc = s.get(ProposedChange, uuid.UUID(result["proposed_change_id"]))
        assert pc.kind == "citation_insert"
        # The anchor falls in section 1 (## Methods), so the row is section-keyed there.
        assert pc.section_index == 1
        assert pc.payload["reference_id"] == str(rid)
        assert pc.payload["anchor"] == ANCHOR
        assert pc.payload["chunk_index"] == 0
        assert pc.payload["evidence_quote"] == "reduces recurrence by 30%"
        assert pc.payload["stance"] == "supports"


@pytest.mark.asyncio
async def test_propose_citation_bad_quote_rejected_no_commit(
    pg_session_factory, _settings_env, tmp_path
):
    ctx, wsid, repo = _build_ctx(pg_session_factory, tmp_path)
    rid = _seed_reference(pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)])
    await read_proposal_impl(ctx)
    before = _commit_count(repo)

    result = await propose_citation_impl(
        ctx,
        reference_id=str(rid),
        anchor=ANCHOR,
        chunk_index=0,
        evidence_quote="this span is not in the chunk at all",
        stance="supports",
    )
    assert "error" in result
    assert _commit_count(repo) == before  # nothing committed
    with pg_session_factory() as s:
        from sqlalchemy import select

        rows = s.scalars(select(ProposedChange).where(ProposedChange.workspace_id == wsid)).all()
        assert rows == []  # no pending row written for an unverifiable citation


@pytest.mark.asyncio
async def test_propose_citation_bad_anchor_rejected(pg_session_factory, _settings_env, tmp_path):
    ctx, wsid, repo = _build_ctx(pg_session_factory, tmp_path)
    rid = _seed_reference(pg_session_factory, wsid, chunks=[(0, "Results", CHUNK_TEXT)])
    await read_proposal_impl(ctx)
    result = await propose_citation_impl(
        ctx,
        reference_id=str(rid),
        anchor="this anchor is nowhere in the proposal",
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
    )
    assert "error" in result
