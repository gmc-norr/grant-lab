"""Direct tests of references *_impl functions. The MCP envelope-wrap
is exercised by test_tools_claude_adapter_wiring; here we test plain
dicts returned by impls."""

import uuid

import pytest

from call_manager.config import Settings
from call_manager.proposal_assist.references.embed import FakeEmbedder
from call_manager.proposal_assist.references.ingest import ingest_bytes
from call_manager.proposal_assist.references.storage import ReferenceStorage
from call_manager.proposal_assist.tools.references import list_references_impl
from call_manager.proposal_assist.tools.registry import ToolContext
from tests.proposal_assist.conftest import make_user, make_workspace


def _make_ctx(pg_session_factory, tmp_path, workspace_id):
    """Build a real ToolContext for impl tests (no MCP envelope, no SDK)."""
    settings = Settings.model_construct(workspaces_dir=str(tmp_path))
    return ToolContext(
        workspace_id=workspace_id,
        repo_dir=tmp_path / str(workspace_id) / "repo",
        session_factory=pg_session_factory,
        settings=settings,
    )


def _seed_three_refs(pg_session_factory, tmp_path):
    """Returns (workspace_id, [reference_id_1, ..., reference_id_3])."""
    storage = ReferenceStorage(root=tmp_path)
    embedder = FakeEmbedder()
    with pg_session_factory() as s:
        user = make_user(s, email=f"owner-{uuid.uuid4()}@ki.se")
        s.commit()
        ws = make_workspace(s, owner=user)
        s.commit()
        wsid, uid = ws.id, user.id

    rids = []
    inputs = [
        ("paper.md", "text/markdown", "paper", b"# CAR-T cells\n\nNotes on therapy."),
        ("analysis.md", "text/markdown", "analysis", b"# Q1 cohort\n\nBaseline data."),
        ("prior.md", "text/markdown", "prior_proposal", b"# 2023 R01\n\nApplication body."),
    ]
    for filename, mime, kind, data in inputs:
        with pg_session_factory() as s:
            result = ingest_bytes(
                session_factory=pg_session_factory,
                session=s,
                storage=storage,
                embedder=embedder,
                workspace_id=wsid,
                kind=kind,
                source_type="uploaded",
                source_uri=None,
                filename=filename,
                mime_type=mime,
                data=data,
                uploaded_by=uid,
            )
            s.commit()
            rids.append(result.reference_id)
    return wsid, rids


@pytest.mark.asyncio
async def test_list_references_no_filter_returns_all(pg_session_factory, tmp_path):
    wsid, rids = _seed_three_refs(pg_session_factory, tmp_path)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)
    result = await list_references_impl(ctx)
    assert "references" in result
    assert len(result["references"]) == 3
    # Each row has the expected metadata keys.
    for row in result["references"]:
        assert set(row.keys()) >= {
            "id",
            "kind",
            "title",
            "filename",
            "text_status",
            "created_at",
        }


@pytest.mark.asyncio
async def test_list_references_filters_by_kind(pg_session_factory, tmp_path):
    wsid, _ = _seed_three_refs(pg_session_factory, tmp_path)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)
    result = await list_references_impl(ctx, kind="paper")
    assert len(result["references"]) == 1
    assert result["references"][0]["kind"] == "paper"


@pytest.mark.asyncio
async def test_list_references_substring_match_on_filename(pg_session_factory, tmp_path):
    wsid, _ = _seed_three_refs(pg_session_factory, tmp_path)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)
    # `q` matches against title or filename (we have no titles set, so
    # only filename will match).
    result = await list_references_impl(ctx, q="prior")
    assert len(result["references"]) == 1
    assert "prior" in result["references"][0]["filename"]


@pytest.mark.asyncio
async def test_list_references_limit_caps_results(pg_session_factory, tmp_path):
    wsid, _ = _seed_three_refs(pg_session_factory, tmp_path)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)
    result = await list_references_impl(ctx, limit=2)
    assert len(result["references"]) == 2


@pytest.mark.asyncio
async def test_list_references_scopes_to_workspace(pg_session_factory, tmp_path):
    """Cross-workspace IDOR check: refs from another workspace must not appear."""
    wsid_a, _ = _seed_three_refs(pg_session_factory, tmp_path)
    wsid_b, _ = _seed_three_refs(pg_session_factory, tmp_path)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid_a)
    result = await list_references_impl(ctx)
    # Only wsid_a's 3 refs visible.
    assert len(result["references"]) == 3
