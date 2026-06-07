"""Citation audit MCP tools (Slice 3 Task 5).

`validate_citations` wires the two audit layers from
``citations/audit.py`` behind a single read-only MCP tool:

- the DETERMINISTIC layer (``validate_citations_deterministic``) always runs —
  pure DB/string re-checks of the canonical ledger against live proposal.md +
  chunks; offline-safe.
- the SEMANTIC layer (``validate_citations_semantic``) runs ONLY when an audit
  LLM (``ctx.citation_service``) is available; it LLM-judges whether each live
  citation's evidence actually backs the claim. When the service is absent (test
  / fake path) or capped, the tool degrades to deterministic-only and signals
  this with ``semantic_available: false``.

Read-only: like ``list_citations`` it holds NO workspace lock. The only writes
are verdict-cache INSERTs, which ``store_verdict`` performs on its OWN
short-lived session (see ``verdict_cache``), so the audit's read session is not
entangled with any write transaction.

The impl never raises — the engine functions are contracted not to, and the
registry's ``_safe_tool`` wrapper is the backstop.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from call_manager.db.models import Workspace
from call_manager.proposal_assist.citations.audit import (
    validate_citations_deterministic,
    validate_citations_semantic,
)
from call_manager.proposal_assist.citations.bibliography import (
    CITATION_STYLES,
    BuildBibliographyBlocked,
    build_bibliography,
)
from call_manager.proposal_assist.citations.claims import iter_proposal_sentences
from call_manager.proposal_assist.citations.markers import find_markers
from call_manager.proposal_assist.tools.citations import _read_proposal_and_ledger

if TYPE_CHECKING:
    from call_manager.proposal_assist.tools.registry import ToolContext

logger = logging.getLogger(__name__)

# System prompt for the uncited-claim judge. The user content is the proposal's
# OWN sentence (first-party) — it is NOT wrapped in <untrusted-reference>, so no
# untrusted-data safety framing is needed here. JSON-only, no prose, so the
# structured client can parse it.
_UNSUPPORTED_SYSTEM = (
    "You audit a single sentence from a research grant proposal. Decide whether "
    "the sentence makes a factual or empirical claim (a statistic, finding, "
    "mechanism, prior result, or assertion about the world) that ought to be "
    "backed by a literature citation. Generic motivation, the authors' own "
    "planned work, aims, hypotheses, and definitions do NOT need a citation. "
    "If a citation is warranted, propose a short literature search query (a few "
    "keywords) that would find supporting references. Respond with a single JSON "
    'object exactly of the form {"needs_citation": bool, "suggested_query": '
    "str} and nothing else. When needs_citation is false, set suggested_query "
    "to an empty string."
)


async def validate_citations_impl(ctx: ToolContext) -> dict[str, Any]:
    """Validate every citation: deterministic checks + (when available) a
    semantic LLM judge. Returns ``{"issues", "semantic_available", "checked"}``
    or an ``{"error": ...}`` dict if proposal.md is missing.
    """
    read = _read_proposal_and_ledger(ctx)
    if isinstance(read, dict):
        return read
    proposal_text, ledger = read

    semantic_available = ctx.citation_service is not None

    with ctx.session_factory() as session:
        det = validate_citations_deterministic(
            proposal_text=proposal_text,
            ledger=ledger,
            session=session,
            workspace_id=ctx.workspace_id,
        )
        if semantic_available:
            sem = validate_citations_semantic(
                proposal_text=proposal_text,
                ledger=ledger,
                session=session,
                session_factory=ctx.session_factory,
                workspace_id=ctx.workspace_id,
                citation_service=ctx.citation_service,
                deterministic_issues=det,
            )
        else:
            sem = []

    checked = len([r for r in (ledger.get("evidence") or []) if r.get("id")])

    return {
        "issues": det + sem,
        "semantic_available": semantic_available,
        "checked": checked,
    }


async def list_unsupported_claims_impl(ctx: ToolContext) -> dict[str, Any]:
    """Heuristic list of factual/empirical proposal sentences lacking a citation.

    Reads proposal.md (reusing ``_read_proposal_and_ledger`` and discarding the
    ledger — the ledger isn't needed; only the live marker positions in the prose
    matter). For every prose sentence that does NOT already carry a citation
    marker, the audit LLM (``ctx.citation_service``) judges whether the sentence
    makes a claim that needs a citation and, if so, proposes a search query.

    A sentence is CITED (and skipped) when any marker's start offset falls within
    its ``[offset, next_offset)`` span — markers conventionally trail the claim
    they support, so the trailing marker lands inside the cited sentence's span.

    Returns ``{"candidates": [...], "semantic_available": bool}`` (or an
    ``{"error": ...}`` dict if proposal.md is missing). ``semantic_available`` is
    False when no audit LLM is configured OR the daily cap was reached mid-run
    (in which case ``candidates`` holds only what was gathered before the cap).
    The deterministic heuristic alone is too noisy, so with no service the list
    is empty rather than every uncited sentence. Never raises.
    """
    read = _read_proposal_and_ledger(ctx)
    if isinstance(read, dict):
        return read
    proposal_text, _ledger = read

    # No audit LLM → return an empty, explicitly-unavailable result. The
    # deterministic-only signal (every uncited sentence) is too noisy to surface.
    if ctx.citation_service is None:
        return {"candidates": [], "semantic_available": False}

    marker_starts = [m["start"] for m in find_markers(proposal_text)]
    sentences = list(iter_proposal_sentences(proposal_text))

    candidates: list[dict[str, Any]] = []
    for i, (sentence_text, offset) in enumerate(sentences):
        # Span = [this offset, next sentence's offset) — last → end of text. A
        # marker whose start falls in the span means this sentence is cited.
        next_offset = sentences[i + 1][1] if i + 1 < len(sentences) else len(proposal_text)
        if any(offset <= ms < next_offset for ms in marker_starts):
            continue  # already cited — skip

        try:
            res = ctx.citation_service.complete_json(
                workspace_id=ctx.workspace_id,
                op="list_unsupported",
                system=_UNSUPPORTED_SYSTEM,
                user=sentence_text,
            )
        except Exception:  # noqa: BLE001 — never let one bad call fail the tool
            logger.warning(
                "list_unsupported_claims: judge call failed for workspace %s; skipping a sentence",
                ctx.workspace_id,
                exc_info=True,
            )
            continue

        if res.cap_exceeded:
            # Cap reached → stop and report partial results as unavailable.
            return {"candidates": candidates, "semantic_available": False}

        data = res.data or {}
        if data.get("needs_citation") is True:
            candidates.append(
                {
                    "location": f"char {offset}",
                    "sentence": sentence_text,
                    "suggested_query": data.get("suggested_query") or "",
                }
            )

    return {"candidates": candidates, "semantic_available": True}


async def build_bibliography_impl(ctx: ToolContext, *, style: str | None = None) -> dict[str, Any]:
    """Render the proposal's cited references into ``bibliography.md`` (fail-closed).

    Resolves the effective style from ``style`` (when given) or the workspace's
    ``citation_style`` setting. Reads proposal.md + the ledger, then calls the
    fail-closed :func:`build_bibliography` engine, which writes ``bibliography.md``
    (a SEPARATE git artifact — never merged into proposal.md). Because it writes,
    it is serialized under the same ``ctx.write_lock`` the mutation tools use
    (the enclosing turn already holds the cross-process pg advisory lock).

    Returns ``{"bibliography_md", "citekey_count", "commit_sha"}`` on success, or
    ``{"error": ..., "blocked": [...]}`` when any cited reference is unapproved /
    unresolved / tombstoned (no partial write), or ``{"error": ...}`` for a bad
    style or a missing proposal.md.
    """
    if style is not None and style not in CITATION_STYLES:
        allowed = ", ".join(sorted(CITATION_STYLES))
        return {"error": f"invalid style {style!r} (allowed: {allowed})"}

    async with ctx.write_lock:
        read = _read_proposal_and_ledger(ctx)
        if isinstance(read, dict):
            return read
        proposal_text, ledger = read

        with ctx.session_factory() as session:
            effective_style = style
            if effective_style is None:
                # citation_style is non-nullable, so a loaded row always has a
                # real value; the only fallback is a workspace deleted mid-turn.
                ws = session.get(Workspace, ctx.workspace_id)
                effective_style = ws.citation_style if ws is not None else "vancouver"

            try:
                return build_bibliography(
                    proposal_text=proposal_text,
                    ledger=ledger,
                    session=session,
                    workspace_id=ctx.workspace_id,
                    style=effective_style,
                    repo_dir=ctx.repo_dir,
                    commit=True,
                )
            except BuildBibliographyBlocked as exc:
                return {
                    "error": "bibliography blocked: unapproved/unresolved references",
                    "blocked": exc.blocked,
                }
