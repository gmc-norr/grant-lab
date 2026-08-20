"""The session- and model-dependent citation core.

Moved out of tools/citations.py in F3d3a, unchanged. It lives here because tool
code may import only the standard library and call_manager.proposal_assist.ports,
and every function below needs a Session, a model, or the repo directory.

Nothing here is converted. F3d3b converts propose_citation_impl (which stayed in
tools/), and the git/filesystem locators these functions name retire with the rest
of the capability holders in F3f.
"""

from __future__ import annotations

import hashlib
import logging
import string
import subprocess
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from call_manager.db.models import Reference, ReferenceChunk
from call_manager.proposal_assist.citations import claims
from call_manager.proposal_assist.citations.audit import claim_drift_hash
from call_manager.proposal_assist.citations.gitio import (
    GitCommitError,
    write_and_commit_files,
)
from call_manager.proposal_assist.citations.ledger import (
    append_evidence,
    build_evidence_row,
    dump_ledger,
    load_ledger,
    new_ev_id,
)
from call_manager.proposal_assist.citations.markers import (
    AnchorError,
    find_markers,
    insert_marker,
)
from call_manager.proposal_assist.citations.quotes import (
    QuoteAmbiguous,
    QuoteNotFound,
    find_quote_offsets,
)
from call_manager.proposal_assist.collab.state import raise_if_collab_severed
from call_manager.proposal_assist.drafting.locking import (
    BUSY_ERROR_MESSAGE,
    DELETED_ERROR_MESSAGE,
    CommitMutexTimeout,
    WorkspaceDeleted,
    acquire_commit_mutex_live,
)
from call_manager.proposal_assist.ports.artifacts import CITATIONS_FILENAME
from call_manager.proposal_assist.references.csl import generate_citekey
from call_manager.proposal_assist.tools.proposal import PROPOSAL_FILENAME

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from call_manager.proposal_assist.tools.registry import ToolContext

logger = logging.getLogger(__name__)

# Spec 2026-06-10 §1 — every git mutation serializes on the per-workspace
# commit mutex; a wedged holder surfaces as this error (web maps it to 503).
_BUSY_ERROR = BUSY_ERROR_MESSAGE  # canonical string lives in locking.py
# The workspace was deleted while the agent was mid-turn (observed under the
# mutex, so nothing was mutated). Same canonical-string discipline.
_DELETED_ERROR = DELETED_ERROR_MESSAGE

# Error string for a transient git-commit failure on the insert path.
# Promoted to a module-level constant so accepting.py can import and branch on it
# (transient git failure -> retryable, NOT terminal stale).
_GIT_COMMIT_FAILED_ERROR = "failed to commit citation"

# Spec line 121: stance ∈ {supports, contradicts, background}.
ALLOWED_STANCES = frozenset({"supports", "contradicts", "background"})
# Spec line 122: confidence ∈ {high, medium, low} (agent self-assessment).
ALLOWED_CONFIDENCE = frozenset({"high", "medium", "low"})


def _validate_stance_confidence(stance: str | None, confidence: str | None) -> dict | None:
    """Return an {"error": ...} dict for an unknown stance/confidence, else None.

    Shared by insert + update so the validation (and the exact error strings)
    stay identical. A value of None means "not specified" and is accepted.
    """
    if stance is not None and stance not in ALLOWED_STANCES:
        return {"error": f"invalid stance {stance!r}; allowed: {sorted(ALLOWED_STANCES)}"}
    if confidence is not None and confidence not in ALLOWED_CONFIDENCE:
        allowed = sorted(ALLOWED_CONFIDENCE)
        return {"error": f"invalid confidence {confidence!r}; allowed: {allowed}"}
    return None


def _read_proposal_and_ledger(ctx: ToolContext) -> tuple[str, dict] | dict:
    """Read proposal.md + the (possibly absent) citations ledger.

    Returns (proposal_text, ledger) on success, or an {"error": ...} dict if
    proposal.md is missing or the ledger fails to parse. Shared by all three
    locked citation functions so the read+error behaviour stays identical.
    """
    proposal_path = ctx.repo_dir / PROPOSAL_FILENAME
    if not proposal_path.exists():
        return {"error": f"{PROPOSAL_FILENAME} does not exist in this workspace"}
    proposal_text = proposal_path.read_text()

    citations_path = ctx.repo_dir / CITATIONS_FILENAME
    ledger_text = citations_path.read_text() if citations_path.exists() else None
    try:
        ledger = load_ledger(ledger_text)
    except ValueError as exc:
        return {"error": f"citations.json is invalid: {exc}"}
    return proposal_text, ledger


def _current_head(repo_dir) -> str:
    """Return the current HEAD sha, or "" if it can't be resolved."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return ""


def _citation_snapshot(csl: dict | None, citekey: str) -> str:
    """Build a short, deterministic human label from CSL (no network).

    Prefers "<first-author-family> <year>"; falls back to the citekey.
    """
    if not isinstance(csl, dict):
        return citekey
    family: str | None = None
    try:
        authors = csl.get("author")
        if authors and isinstance(authors, list) and isinstance(authors[0], dict):
            first = authors[0]
            if first.get("family"):
                family = str(first["family"])
            elif first.get("literal"):
                words = str(first["literal"]).split()
                if words:
                    family = words[-1]
    except Exception:  # noqa: BLE001 — snapshot is best-effort/deterministic only
        family = None

    year: str | None = None
    try:
        issued = csl.get("issued")
        if isinstance(issued, dict):
            date_parts = issued.get("date-parts")
            if (
                date_parts
                and isinstance(date_parts, list)
                and date_parts[0]
                and isinstance(date_parts[0], list)
                and date_parts[0][0] is not None
            ):
                year = str(date_parts[0][0])
    except Exception:  # noqa: BLE001
        year = None

    if family and year:
        return f"{family} {year}"
    if family:
        return family
    return citekey


def _find_idempotent_row(
    ledger: dict,
    *,
    reference_id: str,
    chunk_index: int,
    evidence_quote: str,
    proposal_text: str,
) -> dict | None:
    """Return an existing evidence row identical on (reference_id, chunk_index,
    evidence_quote) whose marker is still present in proposal.md, else None."""
    live_ev_ids = {m["ev_id"] for m in find_markers(proposal_text)}
    for row in ledger["evidence"]:
        # A hand-edited ledger row may be missing 'id'; require a present, truthy
        # id so the return site can subscript it safely (no KeyError on bad rows).
        row_id = row.get("id")
        if (
            row_id
            and row.get("reference_id") == reference_id
            and (row.get("locator") or {}).get("chunk_index") == chunk_index
            and row.get("evidence_quote") == evidence_quote
            and row_id in live_ev_ids
        ):
            return row
    return None


def _resolve_free_citekey(
    base_candidate: str,
    *,
    taken: set[str],
) -> str:
    """Return a citekey not present in ``taken``, self-healing on collision.

    Starts from ``base_candidate``. If it's already taken, strip any trailing
    single a-z suffix to get the stem and try stem+'a'..'z' (so a freshly
    generated "smith2021a" still searches the smith2021 family). If all 26
    suffixes are taken (extraordinarily unlikely), fall back to a ``ref-<hex>``
    key that is free with overwhelming probability (a 12-hex random suffix). It
    is NOT checked against ``taken``, so a collision there is best-effort, not
    guaranteed-impossible; the partial unique index plus reconciliation repair
    any colliding cache write. This is why insert can never hard-error on a
    citekey collision: it always resolves to a key practically free in BOTH the
    ledger and the DB.
    """
    if base_candidate not in taken:
        return base_candidate
    stem = base_candidate
    if len(base_candidate) >= 2 and base_candidate[-1] in string.ascii_lowercase:
        stem = base_candidate[:-1]
    for suffix in string.ascii_lowercase:
        candidate = stem + suffix
        if candidate not in taken:
            return candidate
    return f"ref-{uuid.uuid4().hex[:12]}"


def _resolve_citekey_for_ref(
    session: Session,
    *,
    ledger: dict,
    ref: Reference,
    workspace_id: uuid.UUID,
) -> str:
    """Resolve the citekey to use when citing *ref* — the SINGLE citekey
    resolution shared by the agent insert tool (_insert_citation_locked) and
    the user annotation-insert service (analysis/insert.py).

    Reuse the ref's cached citekey if set; else generate one and self-heal
    against any collision. The DB citekey cache is derived (a best-effort
    write that may have failed), so a generated key can collide with a
    DIFFERENT reference in the ledger and/or DB. Rather than hard-error
    (which could strand THIS insert behind another reference's stale cache),
    bump the suffix to a key free in BOTH the ledger and the DB. A key reused
    from THIS reference (ref.citekey already set) is left exactly as-is.
    """
    if ref.citekey:
        return ref.citekey
    base_candidate = generate_citekey(
        ref.csl_json or ref.csl_json_extracted or {},
        workspace_id,
        session,
    )
    # "taken" = citekeys used in the ledger by ANOTHER reference ∪ citekeys
    # held by a DIFFERENT Reference in this workspace in the DB.
    taken = {
        row.get("citekey")
        for row in ledger["evidence"]
        if row.get("citekey") and row.get("reference_id") != str(ref.id)
    }
    db_citekeys = session.scalars(
        select(Reference.citekey).where(
            Reference.workspace_id == workspace_id,
            Reference.citekey.is_not(None),
            Reference.id != ref.id,
        )
    )
    taken.update(k for k in db_citekeys if k)
    return _resolve_free_citekey(base_candidate, taken=taken)


async def insert_citation_impl(
    ctx: ToolContext,
    *,
    reference_id: str,
    anchor: str,
    chunk_index: int,
    evidence_quote: str,
    stance: str = "supports",
    confidence: str | None = None,
) -> dict[str, Any]:
    """Insert an inline citation marker + evidence row atomically.

    See the module docstring + the Task 5 spec for the full contract. Returns
    {"ev_id", "citekey", "commit_sha"} on success (plus "idempotent": True when
    a duplicate insert was a no-op), or {"error": "..."} on any expected
    failure (leaving NO commit and a clean working tree).
    """
    err = _validate_stance_confidence(stance, confidence)
    if err is not None:
        return err

    try:
        ref_uuid = uuid.UUID(str(reference_id))
    except (ValueError, AttributeError, TypeError):
        return {"error": "invalid reference_id (not a uuid)"}

    # The enclosing drafting turn already holds the per-workspace Postgres
    # advisory lock (controller.launch) for cross-process exclusivity; this
    # asyncio.Lock serializes concurrent tool calls WITHIN a single turn. Do NOT
    # re-acquire the pg advisory lock here — it is connection-scoped and would
    # always fail (the turn holds it on another connection).
    async with ctx.write_lock:
        with ctx.session_factory() as session:
            return _insert_citation_locked(
                ctx,
                session,
                ref_uuid=ref_uuid,
                anchor=anchor,
                chunk_index=chunk_index,
                evidence_quote=evidence_quote,
                stance=stance,
                confidence=confidence,
            )


def compute_citation_insert(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    proposal_text: str,
    ledger: dict,
    ref_uuid: uuid.UUID,
    anchor: str,
    chunk_index: int,
    evidence_quote: str,
    stance: str,
    confidence: str | None,
    quote_start: int | None = None,
    quote_end: int | None = None,
) -> dict[str, Any]:
    """Pure DB-and-compute core of a citation insert (steps 1–12).

    Contract:
    - Takes ``proposal_text`` and ``ledger`` as inputs — no disk reads.
    - Does NOT acquire the commit mutex (caller's responsibility).
    - Does NOT call ``write_and_commit_files`` — no git commit, no disk write.
    - Does NOT mutate ``ref.citekey`` — that is ``_write_citekey_cache``'s job,
      called by the caller AFTER the git commit/checkpoint (Codex High).
    - MAY read ``Reference``/``ReferenceChunk`` rows and resolve the citekey via
      the session.

    Return values:
    - Idempotent hit:  ``{"idempotent": True, "ev_id": str, "citekey": str|None}``
      (no ``ref``, no ``commit_sha`` — caller stamps HEAD).
    - Verify failure:  ``{"error": "<message>"}`` (exact strings preserved).
    - Success:         ``{"new_proposal_text": str, "new_ledger": dict,
                          "ev_id": str, "citekey": str, "ref": Reference}``
      The ``ref`` is the session-attached ``Reference`` so the caller can pass it
      straight to ``_write_citekey_cache``.
    """
    # 1. Load + workspace-scope the Reference.
    ref = session.get(Reference, ref_uuid)
    if ref is None or ref.workspace_id != workspace_id:
        return {"error": "reference not found", "error_code": "ref_not_found"}

    # 1a. A tombstoned reference (soft-deleted) cannot be newly cited — reject
    # BEFORE any write so the working tree stays clean.
    if ref.tombstoned_at is not None:
        return {
            "error": "reference is tombstoned and cannot be cited",
            "error_code": "ref_not_found",
        }

    # 2. Load the ReferenceChunk (reference + index), workspace-scoped.
    chunk = (
        session.query(ReferenceChunk)
        .filter(
            ReferenceChunk.reference_id == ref_uuid,
            ReferenceChunk.chunk_index == chunk_index,
            ReferenceChunk.workspace_id == workspace_id,
        )
        .one_or_none()
    )
    if chunk is None:
        return {"error": "chunk not found", "error_code": "chunk_not_found"}

    # 4. Quote-verify against the raw chunk text.
    if quote_start is not None and quote_end is not None:
        # Exact-offset path (direct human cite): full bounds + equality + non-empty,
        # mirroring human_cite.insert_citation_for_user (slice-equality alone accepts
        # quote_end > len via Python suffix slicing).
        if not (0 <= quote_start < quote_end <= len(chunk.text)):
            return {
                "error": "evidence quote offsets out of range",
                "error_code": "quote_out_of_range",
            }
        start, end = quote_start, quote_end
        sliced = chunk.text[start:end]
        if sliced != evidence_quote:
            return {"error": "evidence quote not found in chunk", "error_code": "quote_not_found"}
        if not sliced.strip():
            return {"error": "evidence quote is empty", "error_code": "quote_empty"}
    else:
        try:
            start, end = find_quote_offsets(chunk.text, evidence_quote)
        except QuoteNotFound:
            return {"error": "evidence quote not found in chunk", "error_code": "quote_not_found"}
        except QuoteAmbiguous:
            return {"error": "evidence quote is ambiguous (occurs multiple times) in chunk",
                    "error_code": "quote_ambiguous"}

    # 5. Idempotency — identical (ref, chunk, quote) with a live marker → no-op.
    # The idempotency key is (reference_id, chunk_index, evidence_quote) BY
    # DESIGN: re-inserting a superseded quote after an update legitimately
    # creates a new citation (the old quote no longer matches any live row).
    existing = _find_idempotent_row(
        ledger,
        reference_id=str(ref_uuid),
        chunk_index=chunk_index,
        evidence_quote=evidence_quote,
        proposal_text=proposal_text,
    )
    if existing is not None:
        # existing["id"] is safe: _find_idempotent_row only matches rows with a
        # present, truthy id. No ref or commit_sha here — caller stamps HEAD.
        return {
            "idempotent": True,
            "ev_id": existing["id"],
            "citekey": existing.get("citekey"),
        }

    # 6. Citekey — the shared resolver: reuse the ref's if set; else generate
    # and self-heal against any ledger/DB collision (see _resolve_citekey_for_ref).
    citekey = _resolve_citekey_for_ref(
        session, ledger=ledger, ref=ref, workspace_id=workspace_id
    )

    # 7. Build the new proposal text (validates anchor uniqueness; nothing written yet).
    ev_id = new_ev_id()
    try:
        new_proposal_text = insert_marker(proposal_text, anchor, citekey, ev_id)
    except AnchorError as exc:
        if "ambiguous" in str(exc):
            return {"error": "anchor is ambiguous", "error_code": "anchor_ambiguous"}
        return {"error": "anchor not found", "error_code": "anchor_not_found"}

    # 8. Hashes — BARE 64-hex, matching references.original_sha256's column format.
    chunk_content_hash = hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
    source_content_hash = ref.original_sha256  # may be None

    # 9. Claim context — the LIVE claim sentence around the just-inserted marker,
    # via the canonical extractor (so a fresh insert and a later reaccept_claim
    # share ONE contract and an insert never immediately looks "drifted"). Fall
    # back to the bare anchor only if extraction degenerates to "".
    inserted = next((m for m in find_markers(new_proposal_text) if m["ev_id"] == ev_id), None)
    if inserted is not None:
        claim_context_text = claims.claim_sentence_around(
            new_proposal_text, inserted["start"], inserted["end"]
        )
    else:
        claim_context_text = ""
    if not claim_context_text:
        claim_context_text = anchor
    # The drift hash MUST be derived via the shared helper so the validator's
    # re-derivation (audit.claim_drift_hash) agrees on the empty-claim fallback.
    claim_context_hash = claim_drift_hash(claim_context_text, anchor)

    # 10. Citation snapshot (deterministic, no network).
    snapshot = _citation_snapshot(ref.csl_json, citekey)

    # 11. Timestamp.
    now = datetime.now(UTC).isoformat()

    # 12. Build the row + new ledger.
    row = build_evidence_row(
        ev_id=ev_id,
        citekey=citekey,
        reference_id=str(ref_uuid),
        chunk_index=chunk_index,
        section_path=chunk.section_path,
        source_content_hash=source_content_hash,
        chunk_content_hash=chunk_content_hash,
        evidence_quote=evidence_quote,
        evidence_quote_offsets=[start, end],
        stance=stance,
        confidence=confidence,
        claim_context_text=claim_context_text,
        claim_context_hash=claim_context_hash,
        citation_snapshot=snapshot,
        created_at=now,
        updated_at=now,
    )
    new_ledger = append_evidence(ledger, row)

    return {
        "new_proposal_text": new_proposal_text,
        "new_ledger": new_ledger,
        "ev_id": ev_id,
        "citekey": citekey,
        "ref": ref,
    }


def _write_citekey_cache(session: Session, ref: Reference, citekey: str, *, owns_txn: bool) -> None:
    """DB cache of the resolved citekey (AFTER git/checkpoint — git ledger is canonical).

    Best-effort: a failure logs and never propagates. owns_txn=True commits here (tool
    path); owns_txn=False uses a SAVEPOINT + flush so a racing IntegrityError cannot
    poison the caller's accept/kernel transaction (the caller commits once).
    """
    if ref.citekey == citekey:
        return
    try:
        if owns_txn:
            ref.citekey = citekey
            session.commit()
        else:
            with session.begin_nested():
                ref.citekey = citekey
                session.flush()
    except Exception:  # noqa: BLE001 — cache write must never fail the caller
        if owns_txn:
            session.rollback()
        logger.warning(
            "insert_citation: references.citekey cache update failed for workspace "
            "%s ref %s citekey %s — reconciliation will backfill",
            ref.workspace_id, ref.id, citekey, exc_info=True,
        )


def _insert_citation_locked(
    ctx: ToolContext,
    session: Session,
    *,
    ref_uuid: uuid.UUID,
    anchor: str,
    chunk_index: int,
    evidence_quote: str,
    stance: str,
    confidence: str | None,
    owns_txn: bool = True,
) -> dict[str, Any]:
    # 0. Serialize against every other git mutation of this workspace (spec
    # 2026-06-10 §1). Transaction-scoped: released by this session's
    # COMMIT/ROLLBACK. Acquired before reading proposal.md for the mutation.
    try:
        # Flag-off-only path: with collab enabled the step-0b severance recheck below RAISES
        # CollabSeveranceConflict (-> 409), so this path is retiring rather than maintained. Slice
        # C3 bounds its tail anyway — the 2s cap now left in force by acquire_commit_mutex applies
        # at every site, including this one, with nothing needed here.
        acquire_commit_mutex_live(session, ctx.workspace_id)
    except CommitMutexTimeout:
        return {"error": _BUSY_ERROR}
    except WorkspaceDeleted:
        # Deleted while the agent was mid-turn. Nothing was mutated; tell the
        # agent plainly rather than crashing the tool.
        session.rollback()
        return {"error": _DELETED_ERROR}

    # 0b. Severance recheck UNDER the mutex (3b-5d.D4 Item 2b follow-up): the
    # accept route's _route() fork read the flag as False, but enable-on-touch
    # may have flipped draft collab on while we blocked on the mutex. RAISES
    # (never returns an error dict) so _accept_citation_insert's terminal-stale
    # mapping is skipped and the ProposedChange row stays PENDING — after the
    # client reloads, the kernel accept path can still accept it. Propagates to
    # _accept_git_first's CollabSeveranceConflict -> 409 handler. The only other
    # caller (insert_citation_impl) is NOT in the MCP tool registry (propose-only
    # world exposes propose_citation), so no live agent path sees this raise.
    # No-op when the flag is off / kill-switch armed (flag-off byte-for-byte).
    raise_if_collab_severed(session, workspace_id=ctx.workspace_id, artifact="draft")

    # 3. Read proposal.md + the (possibly absent) ledger.
    read = _read_proposal_and_ledger(ctx)
    if isinstance(read, dict):
        return read
    proposal_text, ledger = read

    result = compute_citation_insert(
        session,
        workspace_id=ctx.workspace_id,
        proposal_text=proposal_text,
        ledger=ledger,
        ref_uuid=ref_uuid,
        anchor=anchor,
        chunk_index=chunk_index,
        evidence_quote=evidence_quote,
        stance=stance,
        confidence=confidence,
    )
    if "error" in result:
        return result
    if result.get("idempotent"):
        # commit_sha on the idempotent path is the current HEAD (informational —
        # no new commit was made) and may be "" if HEAD resolution fails.
        head = _current_head(ctx.repo_dir)
        if head:
            ctx.agent_view_head = head
        return {"ev_id": result["ev_id"], "citekey": result.get("citekey"),
                "commit_sha": head, "idempotent": True}

    # 13. Atomic commit — proposal.md + citations.json in ONE commit (git first).
    try:
        sha = write_and_commit_files(
            ctx.repo_dir,
            {PROPOSAL_FILENAME: result["new_proposal_text"],
             CITATIONS_FILENAME: dump_ledger(result["new_ledger"])},
            f"cite: add {result['citekey']} [{result['ev_id']}]",
        )
    except GitCommitError:
        logger.exception(
            "insert_citation: atomic commit failed for workspace %s (ev %s)",
            ctx.workspace_id,
            result["ev_id"],
        )
        return {"error": _GIT_COMMIT_FAILED_ERROR}

    # Advance the agent's view of HEAD so a following edit/replace in the same
    # turn doesn't spuriously fail its precondition (spec 2026-06-10 §1).
    # RESIDUAL RISK: advancing the view here re-arms edit/replace preconditions
    # without a proposal re-read.  Safe for these anchor/ev-validated citation
    # tools (the marker text in proposal.md is what we just committed), but it
    # means a replace_proposal immediately after a citation mutation can
    # overwrite changes the agent never saw — accepted residual risk in this
    # slice, mitigated by the system-prompt read-before-replace rule.
    ctx.agent_view_head = sha

    # 14. DB cache (AFTER git) — best-effort; git ledger is canonical.
    _write_citekey_cache(session, result["ref"], result["citekey"], owns_txn=owns_txn)  # step 14

    return {"ev_id": result["ev_id"], "citekey": result["citekey"], "commit_sha": sha}
