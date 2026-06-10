"""Per-job Tier-2 analysis. Spec 2026-06-10 §3.

Citation state decides the branch BEFORE any cache lookup (rev-3 finding 2):
cited occurrences always go through the evidence-ledger semantic audit;
only uncited occurrences use claim_classifications."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from call_manager.db.models import (
    ClaimAnalysisJob,
    ClaimClassification,
    Reference,
    ReferenceChunk,
    UserClaimAnnotation,
)
from call_manager.proposal_assist.analysis.queue import (
    claim_queued_jobs,
    mark_done,
    mark_failed,
    mark_superseded,
    requeue_stuck_running,
    touch_running_job,
)
from call_manager.proposal_assist.citations.audit import validate_citations_semantic
from call_manager.proposal_assist.citations.claims import iter_proposal_sentences
from call_manager.proposal_assist.citations.ledger import load_ledger
from call_manager.proposal_assist.citations.markers import find_markers
from call_manager.proposal_assist.citations.quotes import (
    QuoteAmbiguous,
    QuoteNotFound,
    find_quote_offsets,
)
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.paths import workspace_repo_dir

logger = logging.getLogger(__name__)

ANALYSIS_PROMPT_VERSION = "v1"
TOP_K = 3

CLASSIFY_SYSTEM = (
    "You decide whether a sentence from a grant proposal asserts a checkable "
    "factual claim that needs literature support. Definitions, signposting, and "
    "the proposal's own aims or plans are NOT claims. Reply ONLY with JSON: "
    '{"is_claim": true|false, "reason": "<short>"}'
)
VERDICT_SYSTEM = (
    "You judge whether a reference excerpt supports a claim. Reply ONLY with "
    'JSON: {"verdict": "supports"|"insufficient", "evidence_quote": "<verbatim '
    'quote from the excerpt, or empty>", "rationale": "<short>"}'
)

# Cited verdict issue types that mean the claim's support is wrong. Note:
# "semantic_irrelevant" is deliberately excluded — an irrelevant *background*
# cite is not a wrong claim-support, so it is not surfaced as a claim-analysis
# finding here.
_BAD_CITED_TYPES = ("semantic_unsupported", "semantic_contradicts")


class AnalysisCapExceeded(RuntimeError):
    """The citation LLM token cap was hit mid-analysis; the job should retry."""


@dataclass
class SentenceOcc:
    ordinal: int
    text: str
    offset: int
    ev_ids: list[str] = field(default_factory=list)


def _claim_sha(sentence_text: str) -> str:
    return hashlib.sha256(sentence_text.encode("utf-8")).hexdigest()


def _sentences_with_citation_state(section_text: str) -> list[SentenceOcc]:
    """Split a section into sentences and attach each citation marker's ev id.

    A marker belongs to sentence i if ``offset_i <= marker.start < offset_{i+1}``
    (the last sentence runs to ``len(section_text)``). EDGE: a marker whose start
    precedes the FIRST sentence's offset attaches to sentence 0 — a marker-only
    first line must not silently drop its evidence from the cited set.
    """
    sents = list(iter_proposal_sentences(section_text))
    occs = [SentenceOcc(ordinal=i, text=text, offset=off) for i, (text, off) in enumerate(sents)]
    if not occs:
        return occs

    offsets = [o.offset for o in occs]

    def _assign(marker_start: int) -> int:
        if marker_start < offsets[0]:
            return 0  # marker precedes the first sentence → sentence 0
        for i in range(len(occs)):
            next_off = offsets[i + 1] if i + 1 < len(occs) else len(section_text)
            if offsets[i] <= marker_start < next_off:
                return i
        return len(occs) - 1

    for m in find_markers(section_text):
        occs[_assign(m["start"])].ev_ids.append(m["ev_id"])
    return occs


def _classify(citation_service, workspace_id, sentence: str) -> bool:
    res = citation_service.complete_json(
        workspace_id=workspace_id,
        op="user_claim_classify",
        system=CLASSIFY_SYSTEM,
        user=sentence,
    )
    if res.cap_exceeded:
        raise AnalysisCapExceeded("citation LLM token cap exceeded during classification")
    if res.data is None:
        raise RuntimeError("claim classification returned no data")
    return bool(res.data.get("is_claim"))


def _retrieve_and_judge(
    session,
    citation_service,
    embedder,
    workspace_id,
    claim: str,
    heartbeat: Callable[[], None] | None = None,
) -> dict | None:
    """Embed the claim, judge the top-K chunks, and return the first chunk whose
    'supports' verdict carries a quote that verifies against the chunk text.

    Returns ``{"reference_id", "chunk_index", "evidence_quote", "verdict_json"}``
    or ``None`` if nothing survives. Fails closed: a quote that cannot be located
    (or is ambiguous) in the chunk is discarded.

    ``heartbeat`` is called before EACH per-candidate verdict LLM call so the
    stuck-job reaper does not mistake an active multi-candidate loop for a stale
    job (queue contract: heartbeats BETWEEN LLM calls).
    """
    query_vec = embedder.embed(claim)
    stmt = (
        select(
            ReferenceChunk.reference_id,
            ReferenceChunk.chunk_index,
            ReferenceChunk.text,
        )
        .join(Reference, Reference.id == ReferenceChunk.reference_id)
        .where(
            ReferenceChunk.workspace_id == workspace_id,
            ReferenceChunk.embedding_model == embedder.model,
            Reference.text_status == "done",
            Reference.tombstoned_at.is_(None),
        )
        .order_by(ReferenceChunk.embedding.cosine_distance(query_vec))
        .limit(TOP_K)
    )
    rows = session.execute(stmt).all()
    for row in rows:
        if heartbeat is not None:
            heartbeat()
        res = citation_service.complete_json(
            workspace_id=workspace_id,
            op="user_claim_verdict",
            system=VERDICT_SYSTEM,
            user=f"Claim:\n{claim}\n\nExcerpt:\n{row.text}",
        )
        if res.cap_exceeded:
            raise AnalysisCapExceeded("citation LLM token cap exceeded during verdict")
        data = res.data or {}
        quote = (data.get("evidence_quote") or "").strip()
        if data.get("verdict") != "supports" or not quote:
            continue
        try:
            find_quote_offsets(row.text, quote)
        except (QuoteNotFound, QuoteAmbiguous):
            logger.info(
                "claim-analysis: discarding candidate ref=%s chunk=%s — quote unverifiable",
                row.reference_id,
                row.chunk_index,
            )
            continue
        return {
            "reference_id": row.reference_id,
            "chunk_index": row.chunk_index,
            "evidence_quote": quote,
            "verdict_json": data,
        }
    return None


def _upsert_annotation(session, **kw) -> None:
    """Insert a user_claim_annotation, or reopen a STALE row for this occurrence.

    Resolved rows (open/inserted/dismissed) stay as-is; only rows marked 'stale'
    at fan-out start reopen. This prevents a contradictory strip after a library
    change from silently flipping a user-resolved finding.
    """
    stmt = pg_insert(UserClaimAnnotation).values(**kw)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_user_claim_annotations_occurrence",
        set_={
            "status": "open",
            "reference_id": stmt.excluded.reference_id,
            "chunk_index": stmt.excluded.chunk_index,
            "evidence_quote": stmt.excluded.evidence_quote,
        },
        where=(UserClaimAnnotation.status == "stale"),
    )
    session.execute(stmt)


def _stale_open_annotations_for_section(session, *, workspace_id, section_sha) -> None:
    """Flip every OPEN annotation for this section content to STALE before
    fan-out, so a re-run reopens still-valid findings and leaves obsolete ones
    stale for the reaper/UI to drop."""
    session.query(UserClaimAnnotation).filter(
        UserClaimAnnotation.workspace_id == workspace_id,
        UserClaimAnnotation.section_sha == section_sha,
        UserClaimAnnotation.status == "open",
    ).update({"status": "stale"}, synchronize_session=False)


def _read_optional(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def analyze_job(
    job: ClaimAnalysisJob,
    *,
    session,
    session_factory,
    citation_service,
    embedder,
    workspaces_dir,
) -> None:
    repo = workspace_repo_dir(workspaces_dir, job.workspace_id)
    doc = _read_optional(repo / "proposal.md")
    if doc is None:
        logger.info("claim-analysis: repo/proposal.md missing for job %s → superseded", job.id)
        mark_superseded(session, job)
        return

    sections = split_sections(doc)
    occurrences = [s for s in sections if s.sha == job.section_sha]
    if not occurrences:
        logger.info("claim-analysis: section_sha gone for job %s → superseded", job.id)
        mark_superseded(session, job)
        return

    ledger = load_ledger(_read_optional(repo / "citations.json"))
    section = occurrences[0]
    sentences = _sentences_with_citation_state(section.text)

    # --- CITED FIRST: evidence-ledger semantic audit --------------------- #
    cited_evs: set[str] = set()
    for occ in sentences:
        cited_evs.update(occ.ev_ids)

    bad_by_ev: dict[str, dict] = {}
    if cited_evs:
        # Diff-scoping: validate_citations_semantic judges every row of the
        # ledger it is given, so pass only THIS section's evidence rows.
        scoped_ledger = {
            **ledger,
            "evidence": [r for r in ledger.get("evidence", []) if r.get("id") in cited_evs],
        }
        # Heartbeat before the semantic audit. validate_citations_semantic makes
        # one LLM call per uncached cited row; we cannot thread a per-call
        # callback without changing its signature. The practical protection is:
        # touch_running_job's UPDATE takes a row lock on the job row that is
        # HELD for the whole per-job transaction, and requeue_stuck_running uses
        # FOR UPDATE SKIP LOCKED, so an in-process reaper skips live jobs.
        # The timestamp update itself is uncommitted until job end and matters
        # primarily across process restarts. If the single-instance constraint
        # is ever relaxed, thread a heartbeat callback into
        # validate_citations_semantic.
        touch_running_job(session, job.id)
        semantic_issues = validate_citations_semantic(
            proposal_text=doc,
            ledger=scoped_ledger,
            session=session,
            session_factory=session_factory,
            workspace_id=job.workspace_id,
            citation_service=citation_service,
            deterministic_issues=[],
        )
        if any(i.get("type") == "audit_unavailable" for i in semantic_issues):
            # Retry rather than silently lack findings (cap or LLM error).
            raise RuntimeError("citation audit unavailable (cap or LLM error)")
        bad_by_ev = {
            i["evidence_id"]: i
            for i in semantic_issues
            if i.get("evidence_id") in cited_evs and i.get("type") in _BAD_CITED_TYPES
        }

    # --- UNCITED: cached classification + verified retrieval ------------- #
    uncited_results: dict[str, dict] = {}  # claim_sha -> {"outcome", ...}
    for occ in sentences:
        if occ.ev_ids or not occ.text.strip():
            continue
        sha = _claim_sha(occ.text)
        if sha in uncited_results:
            continue

        existing = (
            session.query(ClaimClassification)
            .filter_by(
                workspace_id=job.workspace_id,
                claim_sha=sha,
                prompt_version=ANALYSIS_PROMPT_VERSION,
            )
            .one_or_none()
        )
        if existing is not None and existing.stale_at is None:
            # Cache hit — costs zero tokens.
            uncited_results[sha] = {
                "outcome": existing.outcome,
                "reference_id": existing.reference_id,
                "chunk_index": existing.chunk_index,
                "evidence_quote": existing.evidence_quote,
            }
            continue

        # Miss or stale → recompute. Heartbeat before EACH classify so the
        # reaper does not mistake a long uncited loop for a stuck job.
        touch_running_job(session, job.id)
        is_claim = _classify(citation_service, job.workspace_id, occ.text)
        found = None
        if is_claim:
            found = _retrieve_and_judge(
                session,
                citation_service,
                embedder,
                job.workspace_id,
                occ.text,
                heartbeat=lambda: touch_running_job(session, job.id),
            )
        if not is_claim:
            outcome = "not_claim"
        elif found is not None:
            outcome = "supported"
        else:
            outcome = "no_match"

        if existing is not None:
            existing.is_claim = is_claim
            existing.outcome = outcome
            existing.reference_id = found["reference_id"] if found else None
            existing.chunk_index = found["chunk_index"] if found else None
            existing.evidence_quote = found["evidence_quote"] if found else None
            existing.verdict_json = found["verdict_json"] if found else None
            existing.stale_at = None
        else:
            # Concurrent insert of the same (workspace, claim_sha,
            # prompt_version) would raise a unique violation → job retry;
            # unreachable under the single-instance constraint.
            session.add(
                ClaimClassification(
                    workspace_id=job.workspace_id,
                    claim_sha=sha,
                    prompt_version=ANALYSIS_PROMPT_VERSION,
                    is_claim=is_claim,
                    outcome=outcome,
                    reference_id=found["reference_id"] if found else None,
                    chunk_index=found["chunk_index"] if found else None,
                    evidence_quote=found["evidence_quote"] if found else None,
                    verdict_json=found["verdict_json"] if found else None,
                )
            )
        session.flush()
        uncited_results[sha] = {
            "outcome": outcome,
            "reference_id": found["reference_id"] if found else None,
            "chunk_index": found["chunk_index"] if found else None,
            "evidence_quote": found["evidence_quote"] if found else None,
        }

    # --- FAN-OUT over every occurrence of this section content ----------- #
    _stale_open_annotations_for_section(
        session, workspace_id=job.workspace_id, section_sha=job.section_sha
    )
    for occ_idx in range(len(occurrences)):
        for occ in sentences:
            common = dict(
                workspace_id=job.workspace_id,
                section_sha=job.section_sha,
                section_occurrence=occ_idx,
                section_heading=section.heading,
                sentence_ordinal=occ.ordinal,
                char_offset=occ.offset,
                claim_sha=_claim_sha(occ.text),
                claim_text=occ.text,
            )
            if occ.ev_ids:
                for ev in occ.ev_ids:
                    if ev in bad_by_ev:
                        _upsert_annotation(
                            session,
                            kind="cited_unsupported",
                            evidence_id=ev,
                            reference_id=None,
                            chunk_index=None,
                            evidence_quote=None,
                            **common,
                        )
                continue
            res = uncited_results.get(_claim_sha(occ.text))
            if res is None or res["outcome"] == "not_claim":
                continue
            if res["outcome"] == "supported":
                _upsert_annotation(
                    session,
                    kind="uncited_supported",
                    evidence_id="",
                    reference_id=res["reference_id"],
                    chunk_index=res["chunk_index"],
                    evidence_quote=res["evidence_quote"],
                    **common,
                )
            else:  # no_match
                _upsert_annotation(
                    session,
                    kind="uncited_no_match",
                    evidence_id="",
                    reference_id=None,
                    chunk_index=None,
                    evidence_quote=None,
                    **common,
                )

    mark_done(session, job)


def drain_analysis_queue(
    *,
    session_factory,
    citation_service,
    embedder,
    workspaces_dir,
    batch_size: int = 3,
) -> dict:
    """Reap stuck jobs, claim a batch, and analyze each in its OWN session
    (per-job commit discipline per the queue docstrings)."""
    with session_factory() as s:
        requeue_stuck_running(s)
        s.commit()

    with session_factory() as s:
        jobs = claim_queued_jobs(s, limit=batch_size)
        s.commit()
        job_ids = [j.id for j in jobs]

    counts = {"done": 0, "failed": 0, "superseded": 0}
    for jid in job_ids:
        with session_factory() as s:
            job = s.get(ClaimAnalysisJob, jid)
            if job is None:
                continue
            try:
                analyze_job(
                    job,
                    session=s,
                    session_factory=session_factory,
                    citation_service=citation_service,
                    embedder=embedder,
                    workspaces_dir=workspaces_dir,
                )
                status = job.status
                s.commit()
                if status == "done":
                    counts["done"] += 1
                elif status == "superseded":
                    counts["superseded"] += 1
            except Exception as exc:
                s.rollback()
                logger.exception("claim-analysis job %s failed", jid)
                with session_factory() as s2:
                    job2 = s2.get(ClaimAnalysisJob, jid)
                    if job2 is not None:
                        mark_failed(s2, job2, error=str(exc))
                        s2.commit()
                counts["failed"] += 1
    return counts
