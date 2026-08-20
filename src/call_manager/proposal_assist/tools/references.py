"""MCP tool implementations for the per-workspace references corpus.

Each *_impl is an async function that takes a ToolContext and returns
a plain dict (registry.py wraps the dict into the MCP envelope). The
six tools added in Slice 2 are read-only — no mutation, no fetch.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select

from call_manager.db.models import Reference, ReferenceChunk
from call_manager.proposal_assist.ports.accounting import MAX_FETCHES_PER_TURN
from call_manager.proposal_assist.references.embed import Embedder
from call_manager.proposal_assist.references.storage import ReferenceStorage
from call_manager.proposal_assist.tools.registry import ToolContext

logger = logging.getLogger(__name__)


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
    return storage.text_path_for(workspace_id=ref.workspace_id, reference_id=ref.id)


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
            .where(
                Reference.workspace_id == ctx.workspace_id,
                # Tombstoned refs are filtered out of discovery (retained only
                # so existing citations keep resolving).
                Reference.tombstoned_at.is_(None),
            )
            .order_by(Reference.created_at.desc())
            .limit(limit)
        )
        if kind:
            stmt = stmt.where(Reference.kind == kind)
        if q:
            like = f"%{q}%"
            stmt = stmt.where(or_(Reference.title.ilike(like), Reference.filename.ilike(like)))
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
    # Clamp top_k to [1, 25]: spec hard cap on the high end;
    # rejecting <=0 here avoids `.limit(-1)` Postgres errors and
    # makes the contract explicit.
    try:
        top_k_int = int(top_k)
    except (TypeError, ValueError):
        top_k_int = 8
    top_k = max(1, min(top_k_int, 25))

    if embedder is None:
        from call_manager.embeddings.client import OllamaEmbedClient
        from call_manager.proposal_assist.references.embed import OllamaEmbedderAdapter

        client = OllamaEmbedClient(
            base_url=ctx.settings.ollama_url,
            model=ctx.settings.embedding_model,
            expected_dim=ctx.settings.embedding_dim,
        )
        embedder = OllamaEmbedderAdapter(client)

    query_vec = embedder.embed(query).vector

    with ctx.session_factory() as s:
        # HNSW cosine distance via pgvector; lower distance = more
        # relevant. Filter to workspace_id (IDOR defense) and the
        # current embedding_model (model-version safety).
        stmt = (
            select(
                ReferenceChunk.reference_id,
                ReferenceChunk.chunk_index,
                ReferenceChunk.section_path,
                ReferenceChunk.text,
                ReferenceChunk.embedding.cosine_distance(query_vec).label("distance"),
                Reference.title,
                Reference.kind,
                Reference.filename,
                Reference.citekey,
            )
            .join(Reference, Reference.id == ReferenceChunk.reference_id)
            .where(
                ReferenceChunk.workspace_id == ctx.workspace_id,
                ReferenceChunk.embedding_model == embedder.model,
                Reference.text_status == "done",
                # Tombstoned refs are excluded from discovery.
                Reference.tombstoned_at.is_(None),
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
        # pgvector cosine_distance is in [0, 2]. Clamp score to [0, 1]
        # so the agent sees a familiar 0-1 similarity scale; fully-
        # opposite vectors get score=0 rather than a confusing negative.
        score = max(0.0, min(1.0, float(1.0 - row.distance)))
        results.append(
            {
                "reference_id": str(row.reference_id),
                "chunk_index": row.chunk_index,
                "title": row.title or row.filename,
                "section_path": row.section_path,
                "snippet": snippet,
                "score": score,
                "citekey": row.citekey,
            }
        )

    # Model-drift visibility: if results are empty AND this workspace
    # has chunks embedded with a different model, surface a warning the
    # agent can relay to the user (instead of silently returning nothing).
    warning = None
    if not results:
        with ctx.session_factory() as s:
            other_models = list(
                s.scalars(
                    select(ReferenceChunk.embedding_model)
                    .where(
                        ReferenceChunk.workspace_id == ctx.workspace_id,
                        ReferenceChunk.embedding_model != embedder.model,
                    )
                    .distinct()
                )
            )
        if other_models:
            warning = (
                f"no results with current embedding_model={embedder.model!r}, "
                f"but this workspace has chunks embedded with "
                f"{other_models!r} — operator may need to run the reembed CLI"
            )
            logger.warning(
                "search_references: model drift detected for workspace=%s; current=%s indexed=%s",
                ctx.workspace_id,
                embedder.model,
                other_models,
            )

    out: dict[str, Any] = {"query": query, "results": results}
    if warning:
        out["warning"] = warning
    return out


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

    # kind='data' routes to summarize_data — agent never gets an outline
    # for tabular data files (per spec rev3 Section 3).
    if ref.kind == "data":
        return {
            "status": "skipped",
            "kind": ref.kind,
            "fallback": "use summarize_data(reference_id) for data files",
        }

    # Status routing per spec rev3 Section 3.
    if ref.text_status in ("pending", "running"):
        return {"status": "indexing"}
    if ref.text_status == "failed":
        return {"status": "failed", "error": ref.text_error}
    if ref.text_status == "skipped":
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
        for part in parts:
            accumulator = (accumulator + " > " + part) if accumulator else part
            if accumulator in nodes_by_path:
                parent_children = nodes_by_path[accumulator]["children"]
                continue
            node = {"section": part, "children": []}
            nodes_by_path[accumulator] = node
            parent_children.append(node)
            parent_children = node["children"]
    return root


async def read_reference_impl(
    ctx: ToolContext,
    *,
    reference_id: str,
    max_chars: int = 20_000,
) -> dict[str, Any]:
    # Clamp max_chars to [1, 100_000]: mirrors the hard caps on
    # search_references (top_k<=25) and list_references (limit<=200) so a
    # caller can't request an unbounded slab of text. Coerce bad input to
    # the default rather than erroring.
    try:
        max_chars_int = int(max_chars)
    except (TypeError, ValueError):
        max_chars_int = 20_000
    max_chars = max(1, min(max_chars_int, 100_000))
    ref = _load_workspace_reference(ctx, reference_id)
    if ref is None:
        return {"error": f"reference {reference_id} not found"}
    # Steer kind=data callers to summarize_data — read_reference would
    # return the schema+sample markdown but the dedicated tool is
    # clearer (and v1.1 may add real schema dicts there).
    if ref.kind == "data":
        return {
            "error": (
                "reference kind is 'data'; use summarize_data(reference_id) "
                "instead — read_reference would only return the same "
                "schema+sample markdown"
            )
        }
    if ref.text_status != "done":
        return {
            "error": (f"reference text status is {ref.text_status!r}; cannot read full text"),
            "text_status": ref.text_status,
            "text_error": ref.text_error,
        }
    text_path = _text_path_for(ctx, ref)
    if not text_path.exists():
        return {"error": "extracted text missing on disk"}
    full = text_path.read_text(encoding="utf-8")
    truncated = len(full) > max_chars
    out: dict[str, Any] = {
        "text": full[:max_chars],
        "truncated": truncated,
        "total_chars": len(full),
    }
    # A tombstoned ref is hidden from discovery (list/search) but its content is
    # retained so an EXISTING citation can still be inspected by id. Flag it so
    # the agent knows it can't cite it afresh.
    if ref.tombstoned_at is not None:
        out["tombstoned"] = True
    return out


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
    # Escape LIKE metacharacters (_ and %) in the user-supplied
    # section_path; otherwise a section name like "Step 1_setup" would
    # over-match via SQL wildcard semantics. Use backslash as escape.
    escaped = section_path.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    with ctx.session_factory() as s:
        stmt = (
            select(ReferenceChunk)
            .where(
                ReferenceChunk.reference_id == ref.id,
                or_(
                    ReferenceChunk.section_path == section_path,
                    ReferenceChunk.section_path.like(f"{escaped} > %", escape="\\"),
                ),
            )
            .order_by(ReferenceChunk.chunk_index)
        )
        chunks = list(s.scalars(stmt))
    if not chunks:
        return {"error": f"no chunks found for section_path {section_path!r}"}
    out: dict[str, Any] = {
        "section_path": section_path,
        "text": "\n\n".join(c.text for c in chunks),
        "chunk_count": len(chunks),
        # `chunks` is fetched ORDER BY chunk_index; sort() makes the ascending
        # invariant explicit so a future query refactor can't silently break it.
        "chunk_indexes": sorted(c.chunk_index for c in chunks),
    }
    # Mirror read_reference_impl: a tombstoned ref is hidden from discovery but
    # its content is retained for existing citations. Flag it so the agent knows
    # it cannot newly cite this reference.
    if ref.tombstoned_at is not None:
        out["tombstoned"] = True
    return out


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


async def fetch_reference_impl(
    ctx: ToolContext,
    *,
    ports,
    invocation,
    input: str,  # noqa: A002
    kind: str = "paper",
    embedder: Embedder | None = None,
) -> dict[str, Any]:
    """Agent-side mid-turn fetch. Atomic per-turn cap enforced BEFORE
    the fetch: UPDATE turn_jobs SET fetch_count = fetch_count + 1
    WHERE workspace_id = ? AND status = 'running' AND fetch_count < 3
    RETURNING fetch_count.

    Validates input shape FIRST (cheap, doesn't cost the budget).
    If a bad input would have wasted a cap slot, the user gets
    nothing while the budget shrinks — bad UX. Detect-then-decrement
    isn't atomic but the test confirms invalid input doesn't increment.

    The `embedder` kwarg matches the search_references_impl test-
    injection seam: production code leaves it None and we construct
    OllamaEmbedClient + OllamaEmbedderAdapter; tests pass a FakeEmbedder
    to avoid needing a running Ollama.
    """
    from call_manager.proposal_assist.references.dispatcher import (
        detect_source_type,
        dispatch_fetch,
    )
    from call_manager.proposal_assist.references.fetcher import FetchError, SSRFError
    from call_manager.proposal_assist.references.ingest import ingest_bytes
    from call_manager.proposal_assist.references.storage import ReferenceStorage

    # Validate input shape first — bad input shouldn't cost the budget.
    try:
        detect_source_type(input)
    except ValueError as e:
        return {"error": str(e)}

    if kind not in ("paper", "prior_proposal", "analysis", "data"):
        return {"error": f"invalid kind: {kind!r}"}

    new_count = ports.accounting.reserve_fetch_slot(invocation.active_turn_job_id)
    if new_count is None:
        # Either no running turn OR cap reached. Same message the tool has always
        # returned; MAX_FETCHES_PER_TURN replaces the literal 3 that used to sit in
        # the WHERE clause AND in this string with nothing keeping them equal.
        return {
            "error": (
                f"fetch cap reached ({MAX_FETCHES_PER_TURN} per turn) OR no running "
                "turn for this workspace — please ask the user to upload the "
                "reference manually via the /references panel."
            )
        }

    def _refund_fetch_slot() -> None:
        """Return a slot reserved but never spent on usable content.

        Bound to the turn that reserved it, never to 'whichever turn is running for
        this workspace': a late refund from a turn the workspace has already moved
        past must not decrement a live turn's count. Unfenced ON PURPOSE (spec 3.3)
        — fencing it would strand jobs holding their slots.
        """
        ports.accounting.refund_fetch_slot(invocation.active_turn_job_id)

    storage = ReferenceStorage(root=Path(ctx.settings.workspaces_dir))
    if embedder is None:
        from call_manager.embeddings.client import OllamaEmbedClient
        from call_manager.proposal_assist.references.embed import OllamaEmbedderAdapter

        embed_client = OllamaEmbedClient(
            base_url=ctx.settings.ollama_url,
            model=ctx.settings.embedding_model,
            expected_dim=ctx.settings.embedding_dim,
        )
        embedder = OllamaEmbedderAdapter(embed_client)

    # Acquire the process-wide ingest limiter (if wired) AFTER the per-turn
    # cap increment but BEFORE the blocking fetch + ingest work. On
    # OverCapacity: refund the turn slot (the fetch never happened) and
    # return a retryable busy error. If no limiter is present (e.g. older
    # test ToolContexts), proceed unguarded.
    from starlette.concurrency import run_in_threadpool

    limiter = getattr(ctx, "reference_ingest_limiter", None)

    _limiter_acquired = False
    if limiter is not None:
        try:
            acquired = limiter.try_acquire()
        except Exception:
            # Limiter broken — proceed unguarded. Critically leave
            # _limiter_acquired False so the finally block does NOT release a
            # slot we never took (which would over-release the semaphore).
            acquired = True
        else:
            _limiter_acquired = acquired
        if not acquired:
            _refund_fetch_slot()
            return {
                "error": "server busy — too many ingests in progress; retry shortly",
                "fetch_count": new_count - 1,
            }

    try:

        def _do_fetch_and_ingest():
            # Do the fetch + ingest. On failure we refund the cap slot above
            # so a transient error doesn't permanently shrink the per-turn budget.
            try:
                data, filename, mime_type, source_meta = dispatch_fetch(input)
            except SSRFError as e:
                return {"_refund": True, "error": f"SSRF refused: {e}"}
            except FetchError as e:
                return {"_refund": True, "error": f"fetch failed: {e}"}

            with ctx.session_factory() as s:
                try:
                    ingest_result = ingest_bytes(
                        session_factory=ctx.session_factory,
                        session=s,
                        storage=storage,
                        embedder=embedder,
                        workspace_id=ctx.workspace_id,
                        kind=kind,
                        source_type=source_meta["source"],
                        source_uri=input,
                        filename=filename,
                        mime_type=mime_type,
                        data=data,
                        uploaded_by=None,  # agent-initiated, no actor_id
                        source_meta=source_meta,
                        citation_service=ctx.citation_service,
                        extract_timeout_seconds=ctx.settings.reference_extract_timeout_seconds,
                    )
                    s.commit()
                except Exception as e:
                    s.rollback()
                    return {"_refund": True, "error": f"ingest failed: {e}"}

            return {
                "reference_id": str(ingest_result.reference_id),
                "filename": filename,
                "source_type": source_meta["source"],
                "fetch_count": new_count,
                "text_status": ingest_result.text_status,
            }

        thread_result = await run_in_threadpool(_do_fetch_and_ingest)
    finally:
        if _limiter_acquired:
            # Defensive: a release() failure must not mask the real result/error.
            with contextlib.suppress(Exception):
                limiter.release()

    if thread_result.get("_refund"):
        _refund_fetch_slot()
        return {"error": thread_result["error"], "fetch_count": new_count - 1}

    return thread_result
