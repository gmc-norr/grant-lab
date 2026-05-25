# References Subsystem — Slice 2 (Read-only MCP Tools) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Agent gets six new MCP tools for browsing + searching + reading the per-workspace reference corpus that Slice 1 made uploadable. Read-only: no mutation, no fetch, no chat composer integration — those are Slices 3 and 4.

**Architecture:** Six new `*_impl` async functions in a new `proposal_assist/tools/references.py` module. They follow the established B5 MCP pattern: take a `ToolContext`, query the DB via `ctx.session_factory()`, return plain dicts. Six new `@tool(...)` registrations in `tools/registry.py:_collect_tools`. Six new entries on the `allowed_tools` allowlist in `claude_adapter.py` (the security boundary). Embeddings reuse the existing `OllamaEmbedClient` via the same pattern as Slice 1's ingest path.

**Tech Stack:** Python 3.12, claude-agent-sdk's `@tool` decorator, SQLAlchemy 2 + pgvector for the HNSW lookup, Ollama via the existing `app.state.embed_client`.

---

## File Structure

**New code:**
- `src/call_manager/proposal_assist/tools/references.py` — six `*_impl` async functions
- `tests/proposal_assist/tools/__init__.py` — empty (may already exist; create only if missing)
- `tests/proposal_assist/tools/test_references_impls.py` — direct tests of each `*_impl` function

**Modified:**
- `src/call_manager/proposal_assist/tools/registry.py` — register six new `@tool` decorators in `_collect_tools`
- `src/call_manager/proposal_assist/drafting/claude_adapter.py` — extend the `allowed_tools` allowlist on line ~127
- `tests/proposal_assist/test_tools_claude_adapter_wiring.py` — assert the new names are in the allowlist (mirrors the existing test that does this for B5's 8 tools)

**Out of scope** (later slices): `fetch_reference` (Slice 3), composer integration + cap pre-counting (Slice 4), delete/copy/reembed-CLI/admin-panel (Slice 5).

---

## Task 1: Skeleton + allowlist + wiring test

**Files:**
- Create: `src/call_manager/proposal_assist/tools/references.py` (skeleton — six stub `*_impl` functions returning `{"error": "not implemented"}`)
- Modify: `src/call_manager/proposal_assist/drafting/claude_adapter.py` — add six tool names to `allowed_tools` list
- Modify: `src/call_manager/proposal_assist/tools/registry.py` — add six `@tool` registrations (calling the stubs)
- Modify: `tests/proposal_assist/test_tools_claude_adapter_wiring.py` — extend the allowlist-coverage assertion

This task gets the plumbing in place so tasks 2-6 can each focus on one tool's impl + test.

- [ ] **Step 1: Read the existing allowlist test pattern**

Run: `grep -n "allowed_tools" /home/parlar_ai/dev/call-monitor/.worktrees/references-slice-2/tests/proposal_assist/test_tools_claude_adapter_wiring.py`

Note the structure — there's an assertion that the allowlist contains the existing 8 names.

- [ ] **Step 2: Write the failing wiring test**

Update `tests/proposal_assist/test_tools_claude_adapter_wiring.py` — find the existing `allowed_tools` assertion. Add the six new names alongside the existing eight:

```python
# Look for the existing assertion like:
#   assert set(captured_options["allowed_tools"]) >= {
#       "read_proposal", "edit_proposal", ... eight tools ...
#   }
# Extend it to also include the six new reference tools:
expected_tools = {
    # existing eight from B5
    "read_proposal", "edit_proposal", "replace_proposal",
    "get_artifact_history", "read_artifact_at", "diff_artifact",
    "get_call_details", "get_profile",
    # six new reference tools (Slice 2)
    "list_references", "search_references", "navigate_reference",
    "read_reference", "read_reference_section", "summarize_data",
}
assert set(captured_options["allowed_tools"]) == expected_tools
```

Adjust the surrounding test code as needed to match the actual existing assertion shape.

- [ ] **Step 3: Run test to confirm it fails**

Run: `uv run pytest tests/proposal_assist/test_tools_claude_adapter_wiring.py -v`

Expected: assertion fails — the six new names aren't in the allowlist yet.

- [ ] **Step 4: Create the skeleton `tools/references.py`**

```python
"""MCP tool implementations for the per-workspace references corpus.

Each *_impl is an async function that takes a ToolContext and returns
a plain dict (registry.py wraps the dict into the MCP envelope). The
six tools added in Slice 2 are read-only — no mutation, no fetch.
"""

from __future__ import annotations

from typing import Any

from call_manager.proposal_assist.tools.registry import ToolContext


async def list_references_impl(
    ctx: ToolContext,
    *,
    kind: str | None = None,
    q: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    return {"error": "not implemented"}


async def search_references_impl(
    ctx: ToolContext,
    *,
    query: str,
    top_k: int = 8,
    kind: str | None = None,
) -> dict[str, Any]:
    return {"error": "not implemented"}


async def navigate_reference_impl(
    ctx: ToolContext,
    *,
    reference_id: str,
) -> dict[str, Any]:
    return {"error": "not implemented"}


async def read_reference_impl(
    ctx: ToolContext,
    *,
    reference_id: str,
    max_chars: int = 20_000,
) -> dict[str, Any]:
    return {"error": "not implemented"}


async def read_reference_section_impl(
    ctx: ToolContext,
    *,
    reference_id: str,
    section_path: str,
) -> dict[str, Any]:
    return {"error": "not implemented"}


async def summarize_data_impl(
    ctx: ToolContext,
    *,
    reference_id: str,
) -> dict[str, Any]:
    return {"error": "not implemented"}
```

- [ ] **Step 5: Register the six new `@tool` decorators in `registry.py`**

In `_collect_tools`, after the existing 8 tools and before the `return [...]` list, add the imports + six new `@tool` blocks. Mirror the JSON-schema-style argument definitions used by the existing tools (each property typed, `required` listed explicitly per the comment on line ~86).

```python
    from call_manager.proposal_assist.tools.references import (
        list_references_impl,
        navigate_reference_impl,
        read_reference_impl,
        read_reference_section_impl,
        search_references_impl,
        summarize_data_impl,
    )

    @tool(
        "list_references",
        "List references in this workspace. Filter by kind "
        "(paper/prior_proposal/analysis/data) or substring match on title. "
        "Returns up to `limit` rows with metadata (id, kind, title, status). "
        "Token-cheap — use at the start of a turn to orient.",
        {
            "type": "object",
            "properties": {
                "kind": {"type": "string"},
                "q": {"type": "string"},
                "limit": {"type": "integer", "default": 50},
            },
            "required": [],
        },
    )
    async def list_references(args):
        return _wrap(
            await list_references_impl(
                ctx,
                kind=args.get("kind"),
                q=args.get("q"),
                limit=int(args.get("limit", 50)),
            )
        )

    @tool(
        "search_references",
        "Semantic search over reference chunks. Embeds the query and "
        "returns the top-k most relevant chunks, each tagged with the "
        "source reference and section_path. Use when you don't know "
        "which reference contains the relevant material.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer", "default": 8},
                "kind": {"type": "string"},
            },
            "required": ["query"],
        },
    )
    async def search_references(args):
        return _wrap(
            await search_references_impl(
                ctx,
                query=args["query"],
                top_k=int(args.get("top_k", 8)),
                kind=args.get("kind"),
            )
        )

    @tool(
        "navigate_reference",
        "Return the chunk-derived outline of a reference (the hierarchy "
        "of section_path values). Use to discover what sections a "
        "reference has before reading any of them.",
        {
            "type": "object",
            "properties": {"reference_id": {"type": "string"}},
            "required": ["reference_id"],
        },
    )
    async def navigate_reference(args):
        return _wrap(
            await navigate_reference_impl(ctx, reference_id=args["reference_id"])
        )

    @tool(
        "read_reference",
        "Return the full extracted text of a reference, truncated at "
        "`max_chars` chars. Use when you want the whole content of a "
        "short reference; use read_reference_section for targeted reads "
        "of long references.",
        {
            "type": "object",
            "properties": {
                "reference_id": {"type": "string"},
                "max_chars": {"type": "integer", "default": 20000},
            },
            "required": ["reference_id"],
        },
    )
    async def read_reference(args):
        return _wrap(
            await read_reference_impl(
                ctx,
                reference_id=args["reference_id"],
                max_chars=int(args.get("max_chars", 20000)),
            )
        )

    @tool(
        "read_reference_section",
        "Return concatenated text of all chunks under a given "
        "section_path of a reference. The path is the breadcrumb "
        "navigate_reference returned (e.g. 'Methods > Statistical analysis').",
        {
            "type": "object",
            "properties": {
                "reference_id": {"type": "string"},
                "section_path": {"type": "string"},
            },
            "required": ["reference_id", "section_path"],
        },
    )
    async def read_reference_section(args):
        return _wrap(
            await read_reference_section_impl(
                ctx,
                reference_id=args["reference_id"],
                section_path=args["section_path"],
            )
        )

    @tool(
        "summarize_data",
        "Return schema + sample (first 5 rows) for a kind='data' "
        "reference (CSV/XLSX/ipynb). Use this instead of read_reference "
        "for data files — the model never needs the raw rows.",
        {
            "type": "object",
            "properties": {"reference_id": {"type": "string"}},
            "required": ["reference_id"],
        },
    )
    async def summarize_data(args):
        return _wrap(
            await summarize_data_impl(ctx, reference_id=args["reference_id"])
        )
```

Then extend the `return [...]` list at the bottom of `_collect_tools` to include the six new callables:

```python
    return [
        # existing eight
        read_proposal, edit_proposal, replace_proposal,
        get_artifact_history, read_artifact_at, diff_artifact,
        get_call_details, get_profile,
        # six new (Slice 2)
        list_references, search_references, navigate_reference,
        read_reference, read_reference_section, summarize_data,
    ]
```

- [ ] **Step 6: Extend `allowed_tools` in `claude_adapter.py`**

Find the `options_kwargs["allowed_tools"] = [...]` block around line 127. Add the six new names:

```python
options_kwargs["allowed_tools"] = [
    "read_proposal",
    "edit_proposal",
    "replace_proposal",
    "get_artifact_history",
    "read_artifact_at",
    "diff_artifact",
    "get_call_details",
    "get_profile",
    # Slice 2 — references
    "list_references",
    "search_references",
    "navigate_reference",
    "read_reference",
    "read_reference_section",
    "summarize_data",
]
```

- [ ] **Step 7: Run tests to confirm wiring is in place**

Run: `uv run pytest tests/proposal_assist/test_tools_claude_adapter_wiring.py -v`

Expected: the allowlist test passes. The full suite should also still be at 987 (no impl tests yet).

Run: `uv run pytest -q`

Expected: 987 passed, 1 skipped.

- [ ] **Step 8: Commit**

```bash
git add src/call_manager/proposal_assist/tools/references.py \
        src/call_manager/proposal_assist/tools/registry.py \
        src/call_manager/proposal_assist/drafting/claude_adapter.py \
        tests/proposal_assist/test_tools_claude_adapter_wiring.py
git commit -m "feat(refs-slice-2): wire skeleton MCP tools + extend allowed_tools allowlist"
```

---

## Task 2: `list_references` implementation

**Files:**
- Modify: `src/call_manager/proposal_assist/tools/references.py`
- Create: `tests/proposal_assist/tools/__init__.py` (only if it doesn't exist)
- Create: `tests/proposal_assist/tools/test_references_impls.py`

- [ ] **Step 1: Write the failing tests**

Check if `tests/proposal_assist/tools/__init__.py` exists; if not, create as empty.

Create `tests/proposal_assist/tools/test_references_impls.py`:

```python
"""Direct tests of references *_impl functions. The MCP envelope-wrap
is exercised by test_tools_claude_adapter_wiring; here we test plain
dicts returned by impls."""

import io
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from call_manager.config import Settings
from call_manager.db.models import Reference, ReferenceChunk
from call_manager.proposal_assist.references.embed import FakeEmbedder
from call_manager.proposal_assist.references.ingest import ingest_bytes
from call_manager.proposal_assist.references.storage import ReferenceStorage
from call_manager.proposal_assist.tools.references import list_references_impl
from call_manager.proposal_assist.tools.registry import ToolContext
from tests.proposal_assist.conftest import make_user, make_workspace


def _make_ctx(pg_session_factory, tmp_path, workspace_id):
    """Build a real ToolContext for impl tests (no MCP envelope, no SDK)."""
    settings = Settings()
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
        user = make_user(s)
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
            "id", "kind", "title", "filename", "text_status", "created_at"
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
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `uv run pytest tests/proposal_assist/tools/test_references_impls.py -v`

Expected: 5 fail — the stub returns `{"error": "not implemented"}`.

- [ ] **Step 3: Implement `list_references_impl`**

Replace the stub in `tools/references.py`:

```python
import uuid

from sqlalchemy import or_, select

from call_manager.db.models import Reference


async def list_references_impl(
    ctx: ToolContext,
    *,
    kind: str | None = None,
    q: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Browse references in the workspace. Token-cheap metadata only."""
    if limit > 200:
        limit = 200  # hard cap
    with ctx.session_factory() as s:
        stmt = (
            select(Reference)
            .where(Reference.workspace_id == ctx.workspace_id)
            .order_by(Reference.created_at.desc())
            .limit(limit)
        )
        if kind:
            stmt = stmt.where(Reference.kind == kind)
        if q:
            like = f"%{q}%"
            stmt = stmt.where(
                or_(Reference.title.ilike(like), Reference.filename.ilike(like))
            )
        rows = list(s.scalars(stmt))
    return {
        "references": [
            {
                "id": str(r.id),
                "kind": r.kind,
                "title": r.title,
                "filename": r.filename,
                "text_status": r.text_status,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]
    }
```

- [ ] **Step 4: Run tests to confirm they pass**

Run: `uv run pytest tests/proposal_assist/tools/test_references_impls.py -v`

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/tools/references.py \
        tests/proposal_assist/tools/__init__.py \
        tests/proposal_assist/tools/test_references_impls.py
git commit -m "feat(refs-slice-2): list_references impl (browse with kind+q filter)"
```

---

## Task 3: `read_reference` + `read_reference_section`

**Files:**
- Modify: `src/call_manager/proposal_assist/tools/references.py`
- Modify: `tests/proposal_assist/tools/test_references_impls.py`

Two tools because they share a common code path (load Reference row, load text from disk or chunks).

- [ ] **Step 1: Write the failing tests**

Append to `tests/proposal_assist/tools/test_references_impls.py`:

```python
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
    assert "CAR-T" in result["text"]
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
        user = make_user(s)
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
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `uv run pytest tests/proposal_assist/tools/test_references_impls.py -k "read_reference" -v`

Expected: 6 fail — both impls still stubbed.

- [ ] **Step 3: Implement `read_reference_impl`**

Add to `tools/references.py`:

```python
from pathlib import Path

from call_manager.proposal_assist.references.storage import ReferenceStorage


def _load_workspace_reference(ctx: ToolContext, reference_id: str) -> Reference | None:
    """Resolve a Reference by ID, scoped to ctx.workspace_id. Returns None
    on any mismatch (unknown id OR cross-workspace IDOR attempt)."""
    try:
        rid = uuid.UUID(reference_id)
    except ValueError:
        return None
    with ctx.session_factory() as s:
        ref = s.get(Reference, rid)
    if ref is None or ref.workspace_id != ctx.workspace_id:
        return None
    return ref


def _text_path_for(ctx: ToolContext, ref: Reference) -> Path:
    storage = ReferenceStorage(root=Path(ctx.settings.workspaces_dir))
    return storage.text_path_for(
        workspace_id=ref.workspace_id, reference_id=ref.id
    )


async def read_reference_impl(
    ctx: ToolContext,
    *,
    reference_id: str,
    max_chars: int = 20_000,
) -> dict[str, Any]:
    ref = _load_workspace_reference(ctx, reference_id)
    if ref is None:
        return {"error": f"reference {reference_id} not found"}
    if ref.text_status != "done":
        return {
            "error": (
                f"reference text status is {ref.text_status!r}; "
                f"cannot read full text"
            ),
            "text_status": ref.text_status,
            "text_error": ref.text_error,
        }
    text_path = _text_path_for(ctx, ref)
    if not text_path.exists():
        return {"error": "extracted text missing on disk"}
    full = text_path.read_text(encoding="utf-8")
    truncated = len(full) > max_chars
    return {
        "text": full[:max_chars],
        "truncated": truncated,
        "total_chars": len(full),
    }
```

- [ ] **Step 4: Implement `read_reference_section_impl`**

Append to `tools/references.py`:

```python
async def read_reference_section_impl(
    ctx: ToolContext,
    *,
    reference_id: str,
    section_path: str,
) -> dict[str, Any]:
    """Return concatenated chunks under a section_path. Matches prefix
    so requesting 'Methods' returns 'Methods' + 'Methods > Stats' +
    'Methods > Cohort'."""
    ref = _load_workspace_reference(ctx, reference_id)
    if ref is None:
        return {"error": f"reference {reference_id} not found"}
    if ref.text_status != "done":
        return {"error": f"reference text status is {ref.text_status!r}"}
    with ctx.session_factory() as s:
        stmt = (
            select(ReferenceChunk)
            .where(
                ReferenceChunk.reference_id == ref.id,
                or_(
                    ReferenceChunk.section_path == section_path,
                    ReferenceChunk.section_path.like(f"{section_path} > %"),
                ),
            )
            .order_by(ReferenceChunk.chunk_index)
        )
        chunks = list(s.scalars(stmt))
    if not chunks:
        return {"error": f"no chunks found for section_path {section_path!r}"}
    return {
        "section_path": section_path,
        "text": "\n\n".join(c.text for c in chunks),
        "chunk_count": len(chunks),
    }
```

You'll need to add `from call_manager.db.models import ReferenceChunk` to the imports.

- [ ] **Step 5: Run tests to confirm they pass**

Run: `uv run pytest tests/proposal_assist/tools/test_references_impls.py -v`

Expected: 11 passed (5 from Task 2 + 6 from Task 3).

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/proposal_assist/tools/references.py \
        tests/proposal_assist/tools/test_references_impls.py
git commit -m "feat(refs-slice-2): read_reference + read_reference_section impls"
```

---

## Task 4: `navigate_reference` (chunk-derived outline)

**Files:**
- Modify: `src/call_manager/proposal_assist/tools/references.py`
- Modify: `tests/proposal_assist/tools/test_references_impls.py`

- [ ] **Step 1: Write the failing tests**

Append:

```python
from call_manager.proposal_assist.tools.references import navigate_reference_impl


@pytest.mark.asyncio
async def test_navigate_reference_returns_hierarchical_outline(
    pg_session_factory, tmp_path
):
    """Markdown with H1 + H2 + H3 should produce a nested tree."""
    storage = ReferenceStorage(root=tmp_path)
    embedder = FakeEmbedder()
    with pg_session_factory() as s:
        user = make_user(s)
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
    """kind='data' rows have outline_status='skipped'; tool returns
    a clear status message instructing the agent to use summarize_data."""
    storage = ReferenceStorage(root=tmp_path)
    embedder = FakeEmbedder()
    csv = b"name,age\nalice,30\nbob,25\n"
    with pg_session_factory() as s:
        user = make_user(s)
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
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `uv run pytest tests/proposal_assist/tools/test_references_impls.py -k "navigate" -v`

Expected: 3 fail.

- [ ] **Step 3: Implement `navigate_reference_impl`**

Append to `tools/references.py`:

```python
async def navigate_reference_impl(
    ctx: ToolContext,
    *,
    reference_id: str,
) -> dict[str, Any]:
    """Derive an outline from DISTINCT section_path values on the
    reference's chunks. v1.1 will override this with PageIndex
    outline.json when outline_status='done'."""
    ref = _load_workspace_reference(ctx, reference_id)
    if ref is None:
        return {"error": f"reference {reference_id} not found"}

    # Status routing per spec rev3 Section 3.
    if ref.text_status in ("pending", "running"):
        return {"status": "indexing"}
    if ref.text_status == "failed":
        return {"status": "failed", "error": ref.text_error}
    if ref.text_status == "skipped":
        # kind=data
        return {
            "status": "skipped",
            "kind": ref.kind,
            "fallback": "use summarize_data(reference_id) for data files",
        }

    # text_status == 'done': build outline from chunk section_paths.
    with ctx.session_factory() as s:
        rows = list(
            s.execute(
                select(ReferenceChunk.section_path)
                .where(ReferenceChunk.reference_id == ref.id)
                .order_by(ReferenceChunk.chunk_index)
            )
        )
    section_paths = [r[0] for r in rows if r[0]]
    outline = _build_outline_tree(section_paths)
    return {
        "status": "done",
        "title": ref.title or ref.filename,
        "outline": outline,
    }


def _build_outline_tree(section_paths: list[str]) -> list[dict[str, Any]]:
    """Convert ['A', 'A > B', 'A > B > C', 'A > D', 'E'] into a tree
    of {section, children: [...]}. Preserves insertion order so the
    rendering reflects chunk order."""
    root: list[dict[str, Any]] = []
    # Track each unique path's node so we can attach children.
    nodes_by_path: dict[str, dict[str, Any]] = {}
    seen_paths: list[str] = []
    for path in section_paths:
        if path not in seen_paths:
            seen_paths.append(path)
    for path in seen_paths:
        parts = [p.strip() for p in path.split(">")]
        accumulator = ""
        parent_children = root
        for i, part in enumerate(parts):
            accumulator = (accumulator + " > " + part) if accumulator else part
            if accumulator in nodes_by_path:
                parent_children = nodes_by_path[accumulator]["children"]
                continue
            node = {"section": part, "children": []}
            nodes_by_path[accumulator] = node
            parent_children.append(node)
            parent_children = node["children"]
    return root
```

- [ ] **Step 4: Run tests to confirm they pass**

Run: `uv run pytest tests/proposal_assist/tools/test_references_impls.py -v`

Expected: 14 passed (5 + 6 + 3).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/tools/references.py \
        tests/proposal_assist/tools/test_references_impls.py
git commit -m "feat(refs-slice-2): navigate_reference impl (chunk-derived outline tree)"
```

---

## Task 5: `summarize_data` for kind='data' refs

**Files:**
- Modify: `src/call_manager/proposal_assist/tools/references.py`
- Modify: `tests/proposal_assist/tools/test_references_impls.py`

- [ ] **Step 1: Write the failing tests**

Append:

```python
from call_manager.proposal_assist.tools.references import summarize_data_impl


@pytest.mark.asyncio
async def test_summarize_data_csv_returns_schema_and_sample(
    pg_session_factory, tmp_path
):
    storage = ReferenceStorage(root=tmp_path)
    embedder = FakeEmbedder()
    csv = b"patient_id,age,diagnosis\n1,42,ALL\n2,67,AML\n3,55,ALL\n"
    with pg_session_factory() as s:
        user = make_user(s)
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
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `uv run pytest tests/proposal_assist/tools/test_references_impls.py -k "summarize" -v`

Expected: 3 fail.

- [ ] **Step 3: Implement `summarize_data_impl`**

Append to `tools/references.py`:

```python
async def summarize_data_impl(
    ctx: ToolContext,
    *,
    reference_id: str,
) -> dict[str, Any]:
    """For kind='data' refs only — return the schema+sample text that
    Stage 1 produced into text.txt (a markdown table of columns + first
    5 rows). The agent uses this instead of read_reference for data
    files so it never sees raw rows."""
    ref = _load_workspace_reference(ctx, reference_id)
    if ref is None:
        return {"error": f"reference {reference_id} not found"}
    if ref.kind != "data":
        return {
            "error": (
                f"reference kind is {ref.kind!r}, not 'data'; "
                f"use read_reference or search_references instead"
            )
        }
    text_path = _text_path_for(ctx, ref)
    if not text_path.exists():
        return {"error": "extracted text missing on disk"}
    return {
        "filename": ref.filename,
        "schema_and_sample": text_path.read_text(encoding="utf-8"),
    }
```

- [ ] **Step 4: Run tests to confirm they pass**

Run: `uv run pytest tests/proposal_assist/tools/test_references_impls.py -v`

Expected: 17 passed.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/tools/references.py \
        tests/proposal_assist/tools/test_references_impls.py
git commit -m "feat(refs-slice-2): summarize_data impl (data-kind only)"
```

---

## Task 6: `search_references` (semantic search via pgvector)

**Files:**
- Modify: `src/call_manager/proposal_assist/tools/references.py`
- Modify: `tests/proposal_assist/tools/test_references_impls.py`

The most complex tool — needs the embed client to embed the query, then a pgvector HNSW lookup.

- [ ] **Step 1: Write the failing tests**

Append:

```python
from call_manager.proposal_assist.tools.references import search_references_impl


@pytest.mark.asyncio
async def test_search_references_returns_top_k_chunks(pg_session_factory, tmp_path):
    """Seed 3 references with different content; embed query that should
    rank the cancer-biopsy one highest. Uses FakeEmbedder so 'top-k'
    is deterministic via the seeded vectors."""
    wsid, rids = _seed_three_refs(pg_session_factory, tmp_path)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)

    # ctx needs an embedder for search — pass via a settings-time injection.
    # For the test we replace the embed_client lookup with a FakeEmbedder.
    # Tools/references.py reads ctx.settings.embedding_model + needs an
    # actual embed client. We pipe in via ctx.session_factory's app_state?
    # No — the cleanest seam is a function arg.
    #
    # See impl below: search_references_impl takes embedder=None (uses
    # OllamaEmbedClient from settings when None) — tests pass FakeEmbedder.
    result = await search_references_impl(
        ctx, query="CAR-T cells", top_k=3, embedder=FakeEmbedder()
    )
    assert "results" in result
    assert len(result["results"]) <= 3
    # Each result has the expected shape.
    for hit in result["results"]:
        assert set(hit.keys()) >= {
            "reference_id", "title", "section_path", "snippet", "score"
        }


@pytest.mark.asyncio
async def test_search_references_filters_by_kind(pg_session_factory, tmp_path):
    """Restrict to kind='paper' — only the paper ref's chunks should
    appear in results."""
    wsid, rids = _seed_three_refs(pg_session_factory, tmp_path)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)
    result = await search_references_impl(
        ctx, query="anything", top_k=20, kind="paper",
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
    result = await search_references_impl(
        ctx, query="anything", top_k=999, embedder=FakeEmbedder()
    )
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
    result = await search_references_impl(
        ctx, query="anything", top_k=50, embedder=FakeEmbedder()
    )
    allowed = {str(r) for r in rids_a}
    for hit in result["results"]:
        assert hit["reference_id"] in allowed
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `uv run pytest tests/proposal_assist/tools/test_references_impls.py -k "search" -v`

Expected: 4 fail.

- [ ] **Step 3: Implement `search_references_impl`**

Append to `tools/references.py`:

```python
from call_manager.proposal_assist.references.embed import (
    Embedder,
    OllamaEmbedderAdapter,
)


async def search_references_impl(
    ctx: ToolContext,
    *,
    query: str,
    top_k: int = 8,
    kind: str | None = None,
    embedder: Embedder | None = None,
) -> dict[str, Any]:
    """Semantic search via pgvector HNSW. Returns the top-k most
    relevant chunks across the workspace's references, optionally
    filtered by kind. embedder defaults to OllamaEmbedClient via
    settings.embedding_model.

    The `embedder` kwarg is the test-injection seam — production code
    leaves it None and the function constructs the adapter."""
    if top_k > 25:
        top_k = 25  # spec hard cap

    if embedder is None:
        from call_manager.embeddings.client import OllamaEmbedClient

        client = OllamaEmbedClient(
            base_url=ctx.settings.ollama_url,
            model=ctx.settings.embedding_model,
        )
        embedder = OllamaEmbedderAdapter(client)

    query_vec = embedder.embed(query)

    with ctx.session_factory() as s:
        # HNSW cosine distance via pgvector; lower distance = more
        # relevant. We filter to workspace_id (B6 IDOR pattern) and
        # the current embedding_model (Codex rev2 model-version safety).
        stmt = (
            select(
                ReferenceChunk.reference_id,
                ReferenceChunk.section_path,
                ReferenceChunk.text,
                ReferenceChunk.embedding.cosine_distance(query_vec).label("distance"),
                Reference.title,
                Reference.kind,
                Reference.filename,
            )
            .join(Reference, Reference.id == ReferenceChunk.reference_id)
            .where(
                ReferenceChunk.workspace_id == ctx.workspace_id,
                ReferenceChunk.embedding_model == embedder.model,
                Reference.text_status == "done",
            )
            .order_by("distance")
            .limit(top_k)
        )
        if kind:
            stmt = stmt.where(Reference.kind == kind)
        rows = list(s.execute(stmt))

    results = []
    for row in rows:
        snippet = row.text[:300] + ("…" if len(row.text) > 300 else "")
        results.append(
            {
                "reference_id": str(row.reference_id),
                "title": row.title or row.filename,
                "section_path": row.section_path,
                "snippet": snippet,
                "score": float(1.0 - row.distance),  # cosine similarity
            }
        )
    return {"query": query, "results": results}
```

- [ ] **Step 4: Run tests to confirm they pass**

Run: `uv run pytest tests/proposal_assist/tools/test_references_impls.py -v`

Expected: 21 passed (17 prior + 4 search).

- [ ] **Step 5: Run the full project suite for no regressions**

Run: `uv run pytest -q`

Expected: 1008 passed, 1 skipped (987 baseline + 21 new tests).

- [ ] **Step 6: ruff format + check**

```bash
uv run ruff format src tests
uv run ruff check src tests
```

Expected: format reports any reformats; check passes.

- [ ] **Step 7: Commit**

```bash
git add src/call_manager/proposal_assist/tools/references.py \
        tests/proposal_assist/tools/test_references_impls.py
git commit -m "feat(refs-slice-2): search_references impl (pgvector HNSW with workspace+model scope)"
```

If ruff format reformatted anything:

```bash
git add -A
git commit -m "style(refs-slice-2): ruff format"
```

---

## Final Verification (manual smoke test)

- [ ] **Step 1: Start the dev server from the worktree**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/references-slice-2
set -a; source /home/parlar_ai/dev/call-monitor/.env; set +a
DATABASE_URL='postgresql+psycopg://call_manager:call_manager@localhost:5432/call_manager' \
APP_BASE_URL=http://127.0.0.1:8001 \
uv run uvicorn call_manager.main:create_app --factory --host 127.0.0.1 --port 8001
```

- [ ] **Step 2: Test agent flow end-to-end**

1. Upload 2-3 reference files via `/workspaces/<id>/references`.
2. Open a workspace chat. Ask: "List the references I've uploaded."
3. Agent should call `list_references` and respond with the file names.
4. Ask: "Search the references for content about X." (use a topic from your uploaded refs)
5. Agent should call `search_references` and quote snippets.
6. Ask: "Show me the outline of reference <title>."
7. Agent should call `navigate_reference` and describe the section tree.

Document outcomes in the merge commit message.

- [ ] **Step 3: Hand off to controller for merge**

The controller will use `superpowers:finishing-a-development-branch`.

---

## Self-review notes

**Spec coverage** — Section 5 Slice 2 lines:
- `list_references` → Task 2 ✓
- `search_references` → Task 6 ✓
- `navigate_reference` → Task 4 ✓
- `read_reference` → Task 3 ✓
- `read_reference_section` → Task 3 ✓
- `summarize_data` → Task 5 ✓
- Register in MCP registry + add to `allowed_tools` allowlist → Task 1 ✓
- Tests for each tool → each task ✓

**Test count target** at end of Slice 2: 1008 passed (987 from Slice 1 baseline + 21 new).

**Status-routing for navigate_reference** matches the spec rev3 Section 3 table: `done` → outline; `pending/running` → `{status: 'indexing'}`; `failed` → `{status, error}`; `skipped` → `{status, kind, fallback}`.

**Cross-workspace IDOR defense** tested for: list, read, search (3 of 6 tools). navigate/read_section/summarize_data rely on `_load_workspace_reference`'s shared filter — the read test covers the same code path indirectly. If concerned, add explicit IDOR tests for the other three in a follow-up.

**No PageIndex** — v1.1 will override `navigate_reference` to use `outline.json` when `outline_status='done'`. v1's chunk-derived outline is the intentional simpler default.
