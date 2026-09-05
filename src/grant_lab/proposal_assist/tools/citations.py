"""The atomic insert_citation tool (Slice 2 Task 5 — the keystone).

insert_citation inserts an inline citation marker into proposal.md AND appends
an evidence row into citations.json in ONE git commit, then best-effort updates
the references.citekey cache. The git ledger is canonical; the DB citekey is a
cache that reconciliation backfills (git-first, cache best-effort — mirrors
proposal.py's contract).

The whole operation runs under the ToolContext's per-context asyncio.Lock so a
concurrent tool call WITHIN the same turn can't interleave between the
quote-verify / marker-build / commit steps. Cross-process exclusivity (one
turn/subprocess per workspace) is already provided by the controller's Postgres
advisory lock + the turn_jobs running-unique index, so the mutation tools must
NOT re-acquire that connection-scoped advisory lock here (the enclosing turn
already holds it on a different connection, so a re-acquire would always fail).

Tools return a plain dict; {"error": "..."} signals failure to the registry's
_wrap, which the SDK turns into an MCP error envelope. The functions never
raise to the caller for expected failure modes.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from grant_lab.db.models import Reference, ReferenceChunk
from grant_lab.proposal_assist.citations.core import _validate_stance_confidence
from grant_lab.proposal_assist.citations.ledger import (
    load_ledger,
)
from grant_lab.proposal_assist.citations.markers import (
    AnchorError,
    anchor_occurrences,
    insert_marker,
)
from grant_lab.proposal_assist.citations.quotes import (
    QuoteAmbiguous,
    QuoteNotFound,
    find_quote_offsets,
)
from grant_lab.proposal_assist.collab import canonical
from grant_lab.proposal_assist.collab.state import is_collab_enabled
from grant_lab.proposal_assist.editing.sectionizer import split_sections
from grant_lab.proposal_assist.ports.effects import ProposeCitationInsert
from grant_lab.proposal_assist.ports.errors import (
    BUSY_ERROR_MESSAGE,
    DELETED_ERROR_MESSAGE,
    CommitMutexTimeout,
    EffectRejected,
    WorkspaceGenerationStale,
)
from grant_lab.proposal_assist.tools.proposal import PROPOSAL_FILENAME

if TYPE_CHECKING:
    from grant_lab.proposal_assist.tools.registry import ToolContext

logger = logging.getLogger(__name__)

CITATIONS_FILENAME = "citations.json"


# --- list_citations (read-only discovery) ----------------------------------


_LIST_CITATIONS_DEFAULT_LIMIT = 100
_LIST_CITATIONS_MAX_LIMIT = 500


async def list_citations_impl(ctx: ToolContext, *, limit: int = 100) -> dict[str, Any]:
    """List citations in the ledger so the agent can discover ev-ids/citekeys.

    Read-only: reads ``citations.json`` directly (no proposal.md, no DB, no git,
    no workspace lock). Turn serialization (one in-flight turn per workspace via
    ``begin_in_flight_exclusive``) means no mutator runs concurrently with this
    read, so a half-written file can't surface; and even a torn read degrades
    safely because ``load_ledger`` treats empty/blank content as an empty ledger.
    An absent ledger yields ``{"citations": [], "truncated": False}``.

    Each entry is a flat projection of an evidence row keyed for the agent:
    ``ev_id`` and ``citekey``, plus the cited reference/chunk, stance,
    confidence, the claim text, and the verbatim evidence quote. ``.get()`` is
    used throughout so a hand-edited row can't crash the listing; a row with no
    ``id`` is skipped (it cannot be acted on by ev-id anyway). Order =
    ledger/document order.

    ``limit`` caps how many rows are projected (default 100, hard cap 500) so a
    workspace with thousands of citations cannot blow up one MCP text block;
    ``truncated`` is ``True`` when at least one more actionable row exists beyond
    the returned page. Mirrors ``list_references``' cap-and-flag contract.
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = _LIST_CITATIONS_DEFAULT_LIMIT
    limit = max(1, min(limit, _LIST_CITATIONS_MAX_LIMIT))

    citations_path = ctx.repo_dir / CITATIONS_FILENAME
    ledger_text = citations_path.read_text() if citations_path.exists() else None
    try:
        ledger = load_ledger(ledger_text)
    except ValueError as exc:
        return {"error": f"citations.json is invalid: {exc}"}

    citations: list[dict[str, Any]] = []
    truncated = False
    for row in ledger["evidence"]:
        ev_id = row.get("id")
        if not ev_id:
            # A hand-edited row with no id can't be targeted by ev-id — skip it.
            continue
        if len(citations) >= limit:
            # At least one more actionable row exists beyond the page.
            truncated = True
            break
        locator = row.get("locator") or {}
        claim_context = row.get("claim_context") or {}
        citations.append(
            {
                "ev_id": ev_id,
                "citekey": row.get("citekey"),
                "reference_id": row.get("reference_id"),
                "chunk_index": locator.get("chunk_index"),
                "section_path": locator.get("section_path"),
                "stance": row.get("stance"),
                "confidence": row.get("confidence"),
                "claim": claim_context.get("text"),
                "evidence_quote": row.get("evidence_quote"),
                "citation_snapshot": row.get("citation_snapshot"),
            }
        )
    return {"citations": citations, "truncated": truncated}


async def propose_citation_impl(
    ctx: ToolContext,
    *,
    ports,
    invocation,
    reference_id: str,
    anchor: str,
    chunk_index: int,
    evidence_quote: str,
    stance: str = "supports",
    confidence: str | None = None,
) -> dict[str, Any]:
    """Propose a citation insert as a PENDING proposed_change (no git commit).

    Runs the propose-time verification subset of the insert checks (the same
    checks _insert_citation_locked runs, MINUS the commit mutex / write / HEAD
    touch), so an unverifiable citation never becomes a pending proposal. Anchor
    existence/uniqueness is verified against the LIVE on-disk proposal (propose-
    time validity), but the section keying (section_index + base_section_sha) is
    taken from the agent's READ-TIME snapshot (ctx.agent_view_proposal /
    ctx.agent_view_sections) so the pair stays consistent if a human added/removed
    a `##` section between read and propose. Then writes the row via
    propose_citation_insert. The full authoritative re-verify happens again at
    accept time under the commit mutex.

    Returns {"proposed_change_id", "status": "pending"} or {"error": ...}.
    """
    err = _validate_stance_confidence(stance, confidence)
    if err is not None:
        return err

    try:
        ref_uuid = uuid.UUID(str(reference_id))
    except (ValueError, AttributeError, TypeError):
        return {"error": "invalid reference_id (not a uuid)"}

    # Read-time base — required for the section row + anti-clobber staleness.
    base_commit_sha = ctx.agent_view_head
    if base_commit_sha is None:
        return {"error": "you haven't read the proposal yet — call read_proposal first"}
    # Read-time proposal snapshot — the section keying (index + base_section_sha)
    # is derived from this, NOT the live on-disk text, so the pair stays consistent
    # if a human added/removed a `##` section between read and propose.
    if ctx.agent_view_proposal is None:
        return {"error": "you haven't read the proposal yet — call read_proposal first"}

    with ctx.session_factory() as session:
        # 1. Load + workspace-scope the Reference (reject missing/foreign).
        ref = session.get(Reference, ref_uuid)
        if ref is None or ref.workspace_id != ctx.workspace_id:
            return {"error": "reference not found"}
        # 1a. Tombstoned reference cannot be newly cited.
        if ref.tombstoned_at is not None:
            return {"error": "reference is tombstoned and cannot be cited"}

        # 2. Load the ReferenceChunk (reference + index), workspace-scoped.
        chunk = (
            session.query(ReferenceChunk)
            .filter(
                ReferenceChunk.reference_id == ref_uuid,
                ReferenceChunk.chunk_index == chunk_index,
                ReferenceChunk.workspace_id == ctx.workspace_id,
            )
            .one_or_none()
        )
        if chunk is None:
            return {"error": "chunk not found"}

        # 3. Read the live proposal for the anchor-uniqueness probe. Collab-on:
        # read the ROOM-CANONICAL text (Slice 3a C1) so an anchor present in the
        # durable log but not yet checkpointed to disk is not false-rejected.
        # Flag-off: raw disk proposal.md, byte-for-byte as before (parity).
        if is_collab_enabled(session, workspace_id=ctx.workspace_id, artifact="draft"):
            proposal_text = canonical.canonical_artifact_markdown(
                session, workspace_id=ctx.workspace_id, artifact="draft", repo_dir=ctx.repo_dir
            )
        else:
            proposal_path = ctx.repo_dir / PROPOSAL_FILENAME
            if not proposal_path.exists():
                return {"error": f"{PROPOSAL_FILENAME} does not exist in this workspace"}
            proposal_text = proposal_path.read_text()

        # 4. Quote-verify against the raw chunk text.
        try:
            find_quote_offsets(chunk.text, evidence_quote)
        except QuoteNotFound:
            return {"error": "evidence quote not found in chunk"}
        except QuoteAmbiguous:
            return {"error": "evidence quote is ambiguous (occurs multiple times) in chunk"}

        # 5. Anchor uniqueness — reuse insert_marker purely to trigger AnchorError
        # on not-found/ambiguous; the mutated text is discarded (we never write it).
        try:
            insert_marker(proposal_text, anchor, "anchorprobe", "ev-000000")
        except AnchorError as exc:
            if "ambiguous" in str(exc):
                return {"error": "anchor is ambiguous"}
            return {"error": "anchor not found"}

        # 6. Map the anchor to the section it falls in (cumulative split offsets),
        # so the citation row is section-keyed for staleness. Both the section
        # split AND the base_section_sha come from the READ-TIME snapshot
        # (ctx.agent_view_proposal / ctx.agent_view_sections), so the
        # (section_index, base_section_sha) pair is internally consistent even if a
        # human added/removed a `##` section on disk between read and propose.
        # Anchor uniqueness is proven in step 5 via insert_marker, which SKIPS
        # anchor occurrences overlapping an existing `[@citekey]<!-- ev -->`
        # marker. The section lookup MUST use the SAME marker-skipping logic
        # (markers.anchor_occurrences) so it can never key the row to a section
        # whose only "occurrence" is the anchor text appearing INSIDE a marker
        # span — a bare `.find()` would land on that marker-internal occurrence
        # and mis-key the row in a proposal that already contains markers.
        read_view = ctx.agent_view_proposal
        read_occurrences = anchor_occurrences(read_view, anchor)
        anchor_offset = read_occurrences[0] if read_occurrences else -1
        if anchor_offset < 0:
            # The anchor exists in the LIVE proposal (step 5 passed) but was NOT
            # in what the agent read — a human added/moved it between read and
            # propose. We cannot key this row to a read-time section; refuse and
            # make the agent re-read (mirrors prose's read-time contract).
            return {"error": "you haven't read the proposal yet — call read_proposal first"}
        sections = split_sections(read_view)
        cursor = 0
        section_index = len(sections) - 1
        for s in sections:
            if anchor_offset < cursor + len(s.text):
                section_index = s.index
                break
            cursor += len(s.text)
        base_section_sha = ctx.agent_view_sections.get(section_index)
        if base_section_sha is None:
            # The agent read the proposal but this section wasn't in its view
            # (it never read that far). Treat as unread — mirror prose's contract.
            return {"error": "you haven't read the proposal yet — call read_proposal first"}

    # The effect runs OUTSIDE the read session's `with` block: the port opens its
    # OWN session and takes the shared lease, so the read session must be closed
    # before it runs (the two-connection inversion port_impl/effects.py's module
    # docstring warns against). reference_id is a uuid.UUID on ProposeCitationInsert,
    # not a str — pass ref_uuid, not str(ref_uuid).
    try:
        pc_id = ports.effects.propose_citation_insert(
            invocation.stamp(),
            ProposeCitationInsert(
                section_index=section_index,
                base_commit_sha=base_commit_sha,
                base_section_sha=base_section_sha,
                reference_id=ref_uuid,
                anchor=anchor,
                chunk_index=chunk_index,
                evidence_quote=evidence_quote,
                stance=stance,
                confidence=confidence,
                provenance=invocation.provenance(),
            ),
        )
    except EffectRejected as e:
        return {"error": str(e)}
    except WorkspaceGenerationStale:
        return {"error": DELETED_ERROR_MESSAGE}
    except CommitMutexTimeout:
        return {"error": BUSY_ERROR_MESSAGE}
    return {"proposed_change_id": str(pc_id), "status": "pending"}
