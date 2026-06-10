"""Spec 2026-06-10 §1 — agent view-of-HEAD precondition + commit mutex.

edit_proposal / replace_proposal must refuse to write when HEAD moved since
the agent last read the proposal (e.g. a concurrent user section save), and
must require a read_proposal before the first mutation. Citation mutations
carry no precondition but must advance the agent's view so a following
edit/replace doesn't spuriously fail.

Exercises the *_impl functions directly (plain dicts, no MCP envelope) over a
real git repo in tmp_path + the pgvector testcontainer (the commit mutex is a
Postgres transaction-scoped advisory lock).
"""

import asyncio
import subprocess
import uuid
from pathlib import Path

import pytest

from call_manager.db.models import Reference, ReferenceChunk
from call_manager.proposal_assist.citations.gitio import current_head
from call_manager.proposal_assist.tools import proposal as _proposal_mod
from call_manager.proposal_assist.tools.citations import insert_citation_impl
from call_manager.proposal_assist.tools.proposal import (
    PROPOSAL_FILENAME,
    edit_proposal_impl,
    read_proposal_impl,
    replace_proposal_impl,
)
from call_manager.proposal_assist.tools.registry import ToolContext
from tests.proposal_assist.conftest import make_workspace

EMBED_DIM = 1024


@pytest.fixture()
def _settings_env(monkeypatch, pg_url):
    """Populate the env vars Settings() requires (mirrors proposal_assist/conftest.py)."""
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")


def _init_repo(repo_dir: Path, initial_content: str = "# Proposal\n\nAlpha line.\n\nBeta line.\n"):
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "agent@test"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=repo_dir, check=True)
    (repo_dir / PROPOSAL_FILENAME).write_text(initial_content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo_dir, check=True)


def _external_user_commit(repo_dir: Path, content: str) -> str:
    """Simulate a user section save: a commit made outside the agent's tools."""
    (repo_dir / PROPOSAL_FILENAME).write_text(content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo_dir, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "user edit: Background (user:1)"],
        cwd=repo_dir,
        check=True,
    )
    return current_head(repo_dir)


def _build_ctx(pg_session_factory, tmp_path) -> tuple[ToolContext, uuid.UUID]:
    from call_manager.config import Settings

    settings = Settings()
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid = ws.id
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir)
    return ToolContext(
        workspace_id=wsid,
        repo_dir=repo_dir,
        session_factory=pg_session_factory,
        settings=settings,
    ), wsid


@pytest.mark.asyncio
async def test_replace_requires_current_view(pg_session_factory, _settings_env, tmp_path):
    ctx, _ = _build_ctx(pg_session_factory, tmp_path)

    # Never read → refused, with a pointer at read_proposal.
    result = await replace_proposal_impl(ctx, content="# New\n", triggering_turn_id=None)
    assert "read_proposal" in result["error"]

    # Read → view tracks HEAD.
    await read_proposal_impl(ctx)
    assert ctx.agent_view_head == current_head(ctx.repo_dir)

    # External (user) commit moves HEAD → replace refused, file untouched.
    user_content = "# Proposal\n\nUser's precious edit.\n"
    _external_user_commit(ctx.repo_dir, user_content)
    result = await replace_proposal_impl(
        ctx, content="# Agent overwrite\n", triggering_turn_id=None
    )
    assert "read_proposal" in result["error"]
    assert (ctx.repo_dir / PROPOSAL_FILENAME).read_text() == user_content

    # Re-read → replace succeeds and advances the view.
    await read_proposal_impl(ctx)
    result = await replace_proposal_impl(
        ctx, content="# Agent overwrite\n", triggering_turn_id=None
    )
    assert result["commit_sha"]
    assert ctx.agent_view_head == result["commit_sha"]
    assert ctx.agent_view_head == current_head(ctx.repo_dir)


@pytest.mark.asyncio
async def test_multi_edit_turn_keeps_working(pg_session_factory, _settings_env, tmp_path):
    """One read, then several sequential edits — the view advances on each
    success, so no re-read is needed inside a multi-edit turn."""
    ctx, _ = _build_ctx(pg_session_factory, tmp_path)
    await read_proposal_impl(ctx)

    first = await edit_proposal_impl(
        ctx,
        edits=[{"old_string": "Alpha line.", "new_string": "Alpha line, improved."}],
        triggering_turn_id=None,
    )
    assert first["commit_sha"]

    second = await edit_proposal_impl(
        ctx,
        edits=[{"old_string": "Beta line.", "new_string": "Beta line, improved."}],
        triggering_turn_id=None,
    )
    assert second["commit_sha"]

    doc = (ctx.repo_dir / PROPOSAL_FILENAME).read_text()
    assert "Alpha line, improved." in doc
    assert "Beta line, improved." in doc


@pytest.mark.asyncio
async def test_concurrent_edits_serialized_no_lost_update(
    pg_session_factory, _settings_env, tmp_path
):
    """Two concurrent edit_proposal calls touching different anchors must
    serialize on ctx.write_lock — both land, neither clobbers the other."""
    ctx, _ = _build_ctx(pg_session_factory, tmp_path)
    await read_proposal_impl(ctx)

    res_a, res_b = await asyncio.gather(
        edit_proposal_impl(
            ctx,
            edits=[{"old_string": "Alpha line.", "new_string": "Alpha CHANGED."}],
            triggering_turn_id=None,
        ),
        edit_proposal_impl(
            ctx,
            edits=[{"old_string": "Beta line.", "new_string": "Beta CHANGED."}],
            triggering_turn_id=None,
        ),
    )
    assert res_a.get("commit_sha"), res_a
    assert res_b.get("commit_sha"), res_b

    doc = (ctx.repo_dir / PROPOSAL_FILENAME).read_text()
    assert "Alpha CHANGED." in doc
    assert "Beta CHANGED." in doc


@pytest.mark.asyncio
async def test_edit_requires_read_first(pg_session_factory, _settings_env, tmp_path):
    ctx, _ = _build_ctx(pg_session_factory, tmp_path)
    result = await edit_proposal_impl(
        ctx,
        edits=[{"old_string": "Alpha line.", "new_string": "X"}],
        triggering_turn_id=None,
    )
    assert "read_proposal" in result["error"]
    # Nothing written.
    assert "Alpha line." in (ctx.repo_dir / PROPOSAL_FILENAME).read_text()


def _seed_reference(pg_session_factory, workspace_id, *, chunk_text: str) -> uuid.UUID:
    """Seed one approved Reference + a single chunk. Minimal copy of the
    citation-insert test fixture (one chunk is all this test needs)."""
    with pg_session_factory() as s:
        ref = Reference(
            workspace_id=workspace_id,
            kind="paper",
            source_type="uploaded",
            filename="seed.pdf",
            storage_path="seed/seed.pdf",
            mime_type="application/pdf",
            csl_json={"author": [{"family": "Smith"}], "issued": {"date-parts": [[2021]]}},
            metadata_status="approved",
        )
        s.add(ref)
        s.flush()
        rid = ref.id
        s.add(
            ReferenceChunk(
                reference_id=rid,
                workspace_id=workspace_id,
                chunk_index=0,
                section_path="Results",
                text=chunk_text,
                embedding=[0.0] * EMBED_DIM,
                embedding_model="fake-embedder",
            )
        )
        s.commit()
    return rid


@pytest.mark.asyncio
async def test_read_proposal_resolves_head_before_reading_content(
    pg_session_factory, _settings_env, tmp_path, monkeypatch
):
    """Fix 1 regression — HEAD must be resolved BEFORE the file is read.

    Without the fix, a user commit between read_text and current_head leaves
    agent_view_head=H2 with content=H1; a subsequent replace then passes its
    stale-HEAD check and silently drops the user's edit (TOCTOU).

    We pin the ORDER deterministically: monkeypatch current_head and
    Path.read_text to record "head" / "read" on each call, then assert
    the recorded order is ["head", "read"].  No real race needed.
    """
    ctx, _ = _build_ctx(pg_session_factory, tmp_path)
    order: list[str] = []

    real_current_head = _proposal_mod.current_head

    def recording_current_head(repo_dir):
        order.append("head")
        return real_current_head(repo_dir)

    real_read_text = Path.read_text

    def recording_read_text(self, *args, **kwargs):
        order.append("read")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(_proposal_mod, "current_head", recording_current_head)
    monkeypatch.setattr(Path, "read_text", recording_read_text)

    result = await read_proposal_impl(ctx)
    assert "error" not in result, result
    # The critical invariant: HEAD resolved first, content read second.
    assert order == ["head", "read"], (
        f"expected ['head', 'read'] but got {order!r} — "
        "current_head must be called before Path.read_text in read_proposal_impl"
    )


@pytest.mark.asyncio
async def test_citation_insert_advances_view(pg_session_factory, _settings_env, tmp_path):
    """insert_citation has no read precondition, but its commit must advance
    ctx.agent_view_head so a following edit/replace doesn't spuriously fail."""
    chunk_text = "Adjuvant therapy reduces recurrence by 30% in the treated cohort."
    # The initial repo proposal already contains a unique "Beta line." anchor.
    anchor = "Beta line."
    ctx, wsid = _build_ctx(pg_session_factory, tmp_path)

    rid = _seed_reference(pg_session_factory, wsid, chunk_text=chunk_text)

    result = await insert_citation_impl(
        ctx,
        reference_id=str(rid),
        anchor=anchor,
        chunk_index=0,
        evidence_quote="reduces recurrence by 30%",
        stance="supports",
    )
    assert "error" not in result, result
    assert result["commit_sha"]
    assert ctx.agent_view_head == result["commit_sha"]
    assert ctx.agent_view_head == current_head(ctx.repo_dir)
