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


from call_manager.proposal_assist.tools.references import (
    read_reference_impl,
    read_reference_section_impl,
)


@pytest.mark.asyncio
async def test_read_reference_returns_full_text(pg_session_factory, tmp_path):
    wsid, rids = _seed_three_refs(pg_session_factory, tmp_path)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)
    result = await read_reference_impl(ctx, reference_id=str(rids[0]))
    assert "text" in result
    # Markdown extractor stores section bodies only (heading text is the
    # breadcrumb on the section, not part of the body). Assert against body.
    assert "Notes on therapy" in result["text"]
    assert result["truncated"] is False


@pytest.mark.asyncio
async def test_read_reference_truncates_at_max_chars(pg_session_factory, tmp_path):
    wsid, rids = _seed_three_refs(pg_session_factory, tmp_path)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)
    result = await read_reference_impl(ctx, reference_id=str(rids[0]), max_chars=10)
    assert len(result["text"]) == 10
    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_read_reference_unknown_id_returns_error(pg_session_factory, tmp_path):
    wsid, _ = _seed_three_refs(pg_session_factory, tmp_path)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)
    result = await read_reference_impl(ctx, reference_id=str(uuid.uuid4()))
    assert "error" in result


@pytest.mark.asyncio
async def test_read_reference_cross_workspace_returns_error(
    pg_session_factory, tmp_path
):
    wsid_a, rids_a = _seed_three_refs(pg_session_factory, tmp_path)
    wsid_b, _ = _seed_three_refs(pg_session_factory, tmp_path)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid_b)
    result = await read_reference_impl(ctx, reference_id=str(rids_a[0]))
    assert "error" in result


@pytest.mark.asyncio
async def test_read_reference_section_returns_concatenated_chunks(
    pg_session_factory, tmp_path
):
    """Seed a markdown with multiple sections under the same parent;
    read_reference_section should return all chunks under that path."""
    storage = ReferenceStorage(root=tmp_path)
    embedder = FakeEmbedder()
    with pg_session_factory() as s:
        user = make_user(s, email=f"owner-{uuid.uuid4()}@ki.se")
        s.commit()
        ws = make_workspace(s, owner=user)
        s.commit()
        wsid, uid = ws.id, user.id

    md = b"""# Doc

## Methods

Methods top text.

### Statistical analysis

Stats body.

### Cohort selection

Cohort body.

## Results

Results body.
"""
    with pg_session_factory() as s:
        result = ingest_bytes(
            session_factory=pg_session_factory,
            session=s,
            storage=storage,
            embedder=embedder,
            workspace_id=wsid,
            kind="paper",
            source_type="uploaded",
            source_uri=None,
            filename="doc.md",
            mime_type="text/markdown",
            data=md,
            uploaded_by=uid,
        )
        s.commit()
        rid = result.reference_id

    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)
    # Request the "Methods" parent — should include the top + both children.
    out = await read_reference_section_impl(
        ctx, reference_id=str(rid), section_path="Doc > Methods"
    )
    assert "text" in out
    assert "Methods top text" in out["text"]
    assert "Stats body" in out["text"]
    assert "Cohort body" in out["text"]
    # Should NOT include Results.
    assert "Results body" not in out["text"]


@pytest.mark.asyncio
async def test_read_reference_section_unknown_path_returns_error(
    pg_session_factory, tmp_path
):
    wsid, rids = _seed_three_refs(pg_session_factory, tmp_path)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)
    result = await read_reference_section_impl(
        ctx, reference_id=str(rids[0]), section_path="Bogus > Path"
    )
    assert "error" in result


from call_manager.proposal_assist.tools.references import navigate_reference_impl


@pytest.mark.asyncio
async def test_navigate_reference_returns_hierarchical_outline(
    pg_session_factory, tmp_path
):
    """Markdown with H1 + H2 + H3 should produce a nested tree."""
    storage = ReferenceStorage(root=tmp_path)
    embedder = FakeEmbedder()
    with pg_session_factory() as s:
        user = make_user(s, email=f"owner-{uuid.uuid4()}@ki.se")
        s.commit()
        ws = make_workspace(s, owner=user)
        s.commit()
        wsid, uid = ws.id, user.id

    md = b"""# Doc

## Methods

Methods top.

### Statistical analysis

Stats.

### Cohort selection

Cohort.

## Results

Results.
"""
    with pg_session_factory() as s:
        result = ingest_bytes(
            session_factory=pg_session_factory,
            session=s,
            storage=storage,
            embedder=embedder,
            workspace_id=wsid,
            kind="paper",
            source_type="uploaded",
            source_uri=None,
            filename="doc.md",
            mime_type="text/markdown",
            data=md,
            uploaded_by=uid,
        )
        s.commit()
        rid = result.reference_id

    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)
    out = await navigate_reference_impl(ctx, reference_id=str(rid))
    assert out["status"] == "done"
    # Outline is a tree of {section, children}.
    outline = out["outline"]
    # Find the "Doc" root node.
    assert any(node["section"] == "Doc" for node in outline)
    doc = next(n for n in outline if n["section"] == "Doc")
    # Doc has two children: Methods and Results.
    child_names = {c["section"] for c in doc["children"]}
    assert "Methods" in child_names
    assert "Results" in child_names
    # Methods has two children: Statistical analysis and Cohort selection.
    methods = next(c for c in doc["children"] if c["section"] == "Methods")
    grand_names = {gc["section"] for gc in methods["children"]}
    assert "Statistical analysis" in grand_names
    assert "Cohort selection" in grand_names


@pytest.mark.asyncio
async def test_navigate_reference_data_kind_returns_skipped(
    pg_session_factory, tmp_path
):
    """kind='data' refs route to the skipped fallback regardless of
    text_status — the agent should be steered to summarize_data."""
    storage = ReferenceStorage(root=tmp_path)
    embedder = FakeEmbedder()
    csv = b"name,age\nalice,30\nbob,25\n"
    with pg_session_factory() as s:
        user = make_user(s, email=f"owner-{uuid.uuid4()}@ki.se")
        s.commit()
        ws = make_workspace(s, owner=user)
        s.commit()
        wsid, uid = ws.id, user.id
        result = ingest_bytes(
            session_factory=pg_session_factory,
            session=s,
            storage=storage,
            embedder=embedder,
            workspace_id=wsid,
            kind="data",
            source_type="uploaded",
            source_uri=None,
            filename="d.csv",
            mime_type="text/csv",
            data=csv,
            uploaded_by=uid,
        )
        s.commit()
        rid = result.reference_id

    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)
    out = await navigate_reference_impl(ctx, reference_id=str(rid))
    assert out["status"] == "skipped"
    assert out["kind"] == "data"
    assert "summarize_data" in out["fallback"]


@pytest.mark.asyncio
async def test_navigate_reference_unknown_id_returns_error(
    pg_session_factory, tmp_path
):
    wsid, _ = _seed_three_refs(pg_session_factory, tmp_path)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)
    out = await navigate_reference_impl(ctx, reference_id=str(uuid.uuid4()))
    assert "error" in out
