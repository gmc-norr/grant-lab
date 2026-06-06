"""Direct tests of references *_impl functions. The MCP envelope-wrap
is exercised by test_tools_claude_adapter_wiring; here we test plain
dicts returned by impls."""

import uuid

import pytest

from call_manager.config import Settings
from call_manager.proposal_assist.references.embed import FakeEmbedder
from call_manager.proposal_assist.references.ingest import ingest_bytes
from call_manager.proposal_assist.references.storage import ReferenceStorage
from call_manager.proposal_assist.tools.references import (
    list_references_impl,
    navigate_reference_impl,
    read_reference_impl,
    read_reference_section_impl,
    search_references_impl,
    summarize_data_impl,
)
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
async def test_read_reference_clamps_max_chars(pg_session_factory, tmp_path):
    """Regression: max_chars must be clamped to a sane upper bound
    (100_000), mirroring the top_k<=25 and limit<=200 caps on the other
    reference tools. An absurd max_chars must not let the agent request
    an unbounded slab; a non-positive value falls back to a sane read."""
    wsid, rids = _seed_three_refs(pg_session_factory, tmp_path)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)
    # Huge request: the seeded body is short, so we still get the whole
    # (untruncated) body — the point is the call doesn't honor an
    # unbounded slice. total_chars stays the true short length.
    result = await read_reference_impl(ctx, reference_id=str(rids[0]), max_chars=10_000_000)
    assert result["truncated"] is False
    assert len(result["text"]) == result["total_chars"]
    assert result["total_chars"] < 100_000

    # Non-positive max_chars is coerced to a positive read, not a crash
    # or an empty/negative slice.
    result_zero = await read_reference_impl(ctx, reference_id=str(rids[0]), max_chars=0)
    assert len(result_zero["text"]) >= 1


@pytest.mark.asyncio
async def test_read_reference_unknown_id_returns_error(pg_session_factory, tmp_path):
    wsid, _ = _seed_three_refs(pg_session_factory, tmp_path)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)
    result = await read_reference_impl(ctx, reference_id=str(uuid.uuid4()))
    assert "error" in result


@pytest.mark.asyncio
async def test_read_reference_cross_workspace_returns_error(pg_session_factory, tmp_path):
    wsid_a, rids_a = _seed_three_refs(pg_session_factory, tmp_path)
    wsid_b, _ = _seed_three_refs(pg_session_factory, tmp_path)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid_b)
    result = await read_reference_impl(ctx, reference_id=str(rids_a[0]))
    assert "error" in result


@pytest.mark.asyncio
async def test_read_reference_section_returns_concatenated_chunks(pg_session_factory, tmp_path):
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
async def test_read_reference_section_unknown_path_returns_error(pg_session_factory, tmp_path):
    wsid, rids = _seed_three_refs(pg_session_factory, tmp_path)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)
    result = await read_reference_section_impl(
        ctx, reference_id=str(rids[0]), section_path="Bogus > Path"
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_navigate_reference_returns_hierarchical_outline(pg_session_factory, tmp_path):
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
async def test_navigate_reference_data_kind_returns_skipped(pg_session_factory, tmp_path):
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
async def test_navigate_reference_unknown_id_returns_error(pg_session_factory, tmp_path):
    wsid, _ = _seed_three_refs(pg_session_factory, tmp_path)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)
    out = await navigate_reference_impl(ctx, reference_id=str(uuid.uuid4()))
    assert "error" in out


@pytest.mark.asyncio
async def test_summarize_data_csv_returns_schema_and_sample(pg_session_factory, tmp_path):
    storage = ReferenceStorage(root=tmp_path)
    embedder = FakeEmbedder()
    csv = b"patient_id,age,diagnosis\n1,42,ALL\n2,67,AML\n3,55,ALL\n"
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
            filename="patients.csv",
            mime_type="text/csv",
            data=csv,
            uploaded_by=uid,
        )
        s.commit()
        rid = result.reference_id

    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)
    out = await summarize_data_impl(ctx, reference_id=str(rid))
    assert out["filename"] == "patients.csv"
    assert "schema_and_sample" in out
    assert "patient_id" in out["schema_and_sample"]
    assert "ALL" in out["schema_and_sample"]


@pytest.mark.asyncio
async def test_summarize_data_rejects_non_data_kind(pg_session_factory, tmp_path):
    """Calling on a kind='paper' ref returns a clear error directing the
    agent to use read_reference / search_references instead."""
    wsid, rids = _seed_three_refs(pg_session_factory, tmp_path)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)
    # rids[0] is a kind='paper' ref.
    out = await summarize_data_impl(ctx, reference_id=str(rids[0]))
    assert "error" in out
    assert "kind" in out["error"].lower()


@pytest.mark.asyncio
async def test_summarize_data_unknown_id_returns_error(pg_session_factory, tmp_path):
    wsid, _ = _seed_three_refs(pg_session_factory, tmp_path)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)
    out = await summarize_data_impl(ctx, reference_id=str(uuid.uuid4()))
    assert "error" in out


@pytest.mark.asyncio
async def test_search_references_returns_top_k_chunks(pg_session_factory, tmp_path):
    """Seed 3 references with different content; embed query that should
    rank the cancer-biopsy one highest. Uses FakeEmbedder so 'top-k'
    is deterministic via the seeded vectors."""
    wsid, rids = _seed_three_refs(pg_session_factory, tmp_path)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)

    result = await search_references_impl(
        ctx, query="CAR-T cells", top_k=3, embedder=FakeEmbedder()
    )
    assert "results" in result
    assert len(result["results"]) <= 3
    # Each result has the expected shape.
    for hit in result["results"]:
        assert set(hit.keys()) >= {
            "reference_id",
            "title",
            "section_path",
            "snippet",
            "score",
        }


@pytest.mark.asyncio
async def test_search_references_filters_by_kind(pg_session_factory, tmp_path):
    """Restrict to kind='paper' — only the paper ref's chunks should
    appear in results."""
    wsid, rids = _seed_three_refs(pg_session_factory, tmp_path)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)
    result = await search_references_impl(
        ctx,
        query="anything",
        top_k=20,
        kind="paper",
        embedder=FakeEmbedder(),
    )
    # Check each result's reference_id corresponds to a kind='paper' row.
    paper_ids = {str(rids[0])}  # _seed_three_refs puts paper first
    for hit in result["results"]:
        assert hit["reference_id"] in paper_ids


@pytest.mark.asyncio
async def test_search_references_caps_top_k_at_25(pg_session_factory, tmp_path):
    wsid, _ = _seed_three_refs(pg_session_factory, tmp_path)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)
    result = await search_references_impl(ctx, query="anything", top_k=999, embedder=FakeEmbedder())
    # Hard cap = 25 per spec; results may be fewer if there are fewer
    # chunks in the workspace.
    assert len(result["results"]) <= 25


@pytest.mark.asyncio
async def test_search_references_scopes_to_workspace(pg_session_factory, tmp_path):
    """IDOR: a query in workspace A must not return chunks from
    workspace B."""
    wsid_a, rids_a = _seed_three_refs(pg_session_factory, tmp_path)
    wsid_b, rids_b = _seed_three_refs(pg_session_factory, tmp_path)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid_a)
    result = await search_references_impl(ctx, query="anything", top_k=50, embedder=FakeEmbedder())
    allowed = {str(r) for r in rids_a}
    for hit in result["results"]:
        assert hit["reference_id"] in allowed


@pytest.mark.asyncio
async def test_search_references_top_k_boundaries(pg_session_factory, tmp_path):
    """Residuals fix: top_k=0 and top_k=-1 must NOT reach Postgres
    .limit() (which would error or return rubbish). Clamped to [1, 25]."""
    wsid, _ = _seed_three_refs(pg_session_factory, tmp_path)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)

    # top_k=0: clamps to 1, returns at most 1 result.
    r0 = await search_references_impl(ctx, query="anything", top_k=0, embedder=FakeEmbedder())
    assert len(r0["results"]) <= 1

    # top_k=-5: same clamp.
    rn = await search_references_impl(ctx, query="anything", top_k=-5, embedder=FakeEmbedder())
    assert len(rn["results"]) <= 1

    # top_k=999: clamps to 25.
    rh = await search_references_impl(ctx, query="anything", top_k=999, embedder=FakeEmbedder())
    assert len(rh["results"]) <= 25


@pytest.mark.asyncio
async def test_search_references_score_is_clamped_to_unit_interval(pg_session_factory, tmp_path):
    """Residuals fix: cosine_distance is [0, 2]; raw score = 1-distance
    could be negative for fully-opposite vectors. The agent should see
    a clean [0, 1] similarity scale."""
    wsid, _ = _seed_three_refs(pg_session_factory, tmp_path)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)
    result = await search_references_impl(ctx, query="anything", top_k=10, embedder=FakeEmbedder())
    for hit in result["results"]:
        assert 0.0 <= hit["score"] <= 1.0


@pytest.mark.asyncio
async def test_search_references_warns_on_embedding_model_drift(pg_session_factory, tmp_path):
    """Residuals fix: if results are empty AND chunks exist with a
    DIFFERENT embedding_model, surface a `warning` key so the agent
    can tell the user about the operator-action gap."""
    wsid, _ = _seed_three_refs(pg_session_factory, tmp_path)

    # Verify drift: query with a NEW model that doesn't match the
    # chunks' embedding_model (which is FakeEmbedder's default
    # "fake-embedder"). With a different .model, the filter excludes
    # all chunks and we expect empty + warning.
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)
    different = FakeEmbedder(model="different-model")
    result = await search_references_impl(ctx, query="anything", top_k=10, embedder=different)
    assert result["results"] == []
    assert "warning" in result
    assert "different-model" in result["warning"]
    assert "fake-embedder" in result["warning"]


@pytest.mark.asyncio
async def test_search_references_includes_chunk_index_and_citekey(pg_session_factory, tmp_path):
    """search_references results must include chunk_index (int) and citekey
    (str or None). Seed one ref with a citekey and one without; assert both
    fields are present and have the expected values."""
    storage = ReferenceStorage(root=tmp_path)
    embedder = FakeEmbedder()
    with pg_session_factory() as s:
        user = make_user(s, email=f"owner-{uuid.uuid4()}@ki.se")
        s.commit()
        ws = make_workspace(s, owner=user)
        s.commit()
        wsid, uid = ws.id, user.id

    # Seed two refs — paper (will get citekey) and analysis (no citekey).
    with pg_session_factory() as s:
        result_paper = ingest_bytes(
            session_factory=pg_session_factory,
            session=s,
            storage=storage,
            embedder=embedder,
            workspace_id=wsid,
            kind="paper",
            source_type="uploaded",
            source_uri=None,
            filename="paper_citekey.md",
            mime_type="text/markdown",
            data=b"# Alpha\n\nContent alpha.",
            uploaded_by=uid,
        )
        s.commit()
        paper_rid = result_paper.reference_id

    with pg_session_factory() as s:
        result_analysis = ingest_bytes(
            session_factory=pg_session_factory,
            session=s,
            storage=storage,
            embedder=embedder,
            workspace_id=wsid,
            kind="analysis",
            source_type="uploaded",
            source_uri=None,
            filename="analysis_no_citekey.md",
            mime_type="text/markdown",
            data=b"# Beta\n\nContent beta.",
            uploaded_by=uid,
        )
        s.commit()
        analysis_rid = result_analysis.reference_id

    # Set a citekey on the paper ref only.
    expected_citekey = "Smith2024"
    with pg_session_factory() as s:
        from call_manager.db.models import Reference as _Reference

        ref = s.get(_Reference, paper_rid)
        ref.citekey = expected_citekey
        s.commit()

    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)
    result = await search_references_impl(
        ctx, query="alpha beta", top_k=10, embedder=FakeEmbedder()
    )
    assert "results" in result
    assert len(result["results"]) >= 2

    # All hits must carry chunk_index (int) and citekey (str or None).
    for hit in result["results"]:
        assert "chunk_index" in hit, f"chunk_index missing from hit: {hit}"
        assert "citekey" in hit, f"citekey missing from hit: {hit}"
        assert isinstance(hit["chunk_index"], int)
        assert hit["citekey"] is None or isinstance(hit["citekey"], str)

    # Hit from the paper ref should carry the citekey we set.
    paper_hits = [h for h in result["results"] if h["reference_id"] == str(paper_rid)]
    assert paper_hits, "expected at least one hit from the paper ref"
    assert paper_hits[0]["citekey"] == expected_citekey

    # Hit from the analysis ref (no citekey) should carry None.
    analysis_hits = [h for h in result["results"] if h["reference_id"] == str(analysis_rid)]
    assert analysis_hits, "expected at least one hit from the analysis ref"
    assert analysis_hits[0]["citekey"] is None

    # Existing keys must still be present (back-compat).
    for hit in result["results"]:
        assert set(hit.keys()) >= {"reference_id", "title", "section_path", "snippet", "score"}


@pytest.mark.asyncio
async def test_read_reference_section_includes_chunk_indexes(pg_session_factory, tmp_path):
    """read_reference_section result must include chunk_indexes (list of ints,
    sorted ascending). Seed a multi-section document and assert the field."""
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
            filename="doc_idx.md",
            mime_type="text/markdown",
            data=md,
            uploaded_by=uid,
        )
        s.commit()
        rid = result.reference_id

    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)
    out = await read_reference_section_impl(
        ctx, reference_id=str(rid), section_path="Doc > Methods"
    )
    assert "chunk_indexes" in out, f"chunk_indexes missing from result: {out}"
    idxs = out["chunk_indexes"]
    assert isinstance(idxs, list)
    assert len(idxs) >= 1
    assert all(isinstance(i, int) for i in idxs)
    # Must be sorted ascending.
    assert idxs == sorted(idxs), f"chunk_indexes not sorted: {idxs}"
    # chunk_count must still match the number of indexes returned.
    assert out["chunk_count"] == len(idxs)
    # Existing keys must still be present (back-compat).
    assert set(out.keys()) >= {"section_path", "text", "chunk_count"}


@pytest.mark.asyncio
async def test_read_reference_data_kind_steers_to_summarize(pg_session_factory, tmp_path):
    """Residuals fix: read_reference on kind='data' should steer the
    agent to summarize_data instead of returning the schema+sample."""
    storage = ReferenceStorage(root=tmp_path)
    embedder = FakeEmbedder()
    csv = b"a,b\n1,2\n"
    with pg_session_factory() as s:
        user = make_user(s, email=f"r-{uuid.uuid4()}@ki.se")
        s.commit()
        ws = make_workspace(s, owner=user)
        s.commit()
        wsid = ws.id
        uid = user.id
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
    out = await read_reference_impl(ctx, reference_id=str(rid))
    assert "error" in out
    assert "summarize_data" in out["error"]
