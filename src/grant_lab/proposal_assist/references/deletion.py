"""Delete-vs-tombstone policy for references (Slice 2 Task 8).

A reference that is cited by ANY evidence row in the workspace's citations.json
must NOT be hard-deleted — doing so would orphan those citations and break
resolution. Instead it is *tombstoned* (``references.tombstoned_at`` set):
bytes, chunks, and metadata are retained so existing citations keep resolving,
while the reference is filtered out of discovery (list/search) and can no longer
be newly cited (see ``insert_citation_impl``).

An UNCITED reference is hard-deleted: its chunks + row are removed from the DB
and its stored bytes are removed via ``ReferenceStorage.delete_one``.

NOTE: at the time this helper was written there was no user-facing
reference-delete route in the codebase (only ``storage.delete_one`` used during
ingest rollback). This helper is therefore the first deliberate reference
deleter; UI/route wiring is expected to call it.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from grant_lab.db.models import Reference, ReferenceChunk, WorkspaceEvent
from grant_lab.proposal_assist.analysis.invalidation import on_reference_unavailable
from grant_lab.proposal_assist.citations.ledger import load_ledger
from grant_lab.proposal_assist.ports.artifacts import CITATIONS_FILENAME

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from grant_lab.proposal_assist.references.storage import ReferenceStorage

logger = logging.getLogger(__name__)


def delete_or_tombstone_reference(
    *,
    session: Session,
    repo_dir: Path,
    reference_id: uuid.UUID | str,
    workspace_id: uuid.UUID,
    storage: ReferenceStorage | None = None,
    actor_id: int | None = None,
) -> str:
    """Tombstone a cited reference or hard-delete an uncited one.

    Returns ``"tombstoned"`` or ``"deleted"``.

    Raises ``ValueError`` if the reference does not exist or belongs to a
    different workspace (IDOR defense — a cross-workspace id is treated as
    not-found).

    The cited check reads ``repo_dir / citations.json`` and looks for any
    evidence row whose ``reference_id`` equals this reference's id. An absent or
    empty ledger means "uncited".

    ``storage`` is the byte-cleanup seam. When ``None`` (and a hard-delete is
    needed) only the DB rows are removed; callers that own the filesystem should
    pass a ``ReferenceStorage`` so the stored bytes are removed too.

    A ``WorkspaceEvent`` audit row (``kind="reference.tombstoned"`` or
    ``kind="reference.deleted"``) is written inside the same transaction as the
    delete/tombstone, guaranteeing atomic audit coverage. Pass ``actor_id`` to
    record which user performed the action.
    """
    try:
        rid = uuid.UUID(str(reference_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"invalid reference_id: {reference_id!r}") from exc

    ref = session.get(Reference, rid)
    if ref is None or ref.workspace_id != workspace_id:
        # Unknown id OR cross-workspace attempt — treat both as not-found.
        raise ValueError(f"reference {rid} not found in workspace {workspace_id}")

    # Workspaces root for the claim-analysis invalidation hook: storage.root IS
    # the workspaces_dir when storage is provided; otherwise invert the
    # production layout <workspaces_dir>/<workspace_id>/repo (paths.
    # workspace_repo_dir). A wrong derivation only suppresses the re-enqueue
    # (no proposal.md found) — the DB staleing inside the hook is unaffected.
    workspaces_dir = storage.root if storage is not None else Path(repo_dir).parent.parent

    if _is_cited(repo_dir, rid):
        # Cited → tombstone: retain bytes/chunks/metadata so existing citations
        # keep resolving. Idempotent: re-tombstoning just refreshes nothing
        # material (we leave an existing timestamp untouched).
        if ref.tombstoned_at is None:
            ref.tombstoned_at = datetime.now(UTC)
            session.add(
                WorkspaceEvent(
                    workspace_id=workspace_id,
                    kind="reference.tombstoned",
                    actor_id=actor_id,
                    payload={"reference_id": str(rid), "outcome": "tombstoned"},
                )
            )
        # Library shrank (spec 2026-06-10 §3): stale supported findings that
        # point at this reference and re-enqueue their sections. Same
        # transaction as the tombstone — one commit covers both.
        try:
            on_reference_unavailable(
                session,
                workspace_id=workspace_id,
                reference_id=rid,
                workspaces_dir=workspaces_dir,
            )
        except Exception:  # noqa: BLE001 — invalidation is best-effort
            logger.exception("claim-analysis invalidation failed (tombstone)")
        session.commit()
        return "tombstoned"

    # Library shrank (spec 2026-06-10 §3) — MUST run BEFORE session.delete(ref):
    # the FKs on ClaimClassification.reference_id / UserClaimAnnotation
    # .reference_id are SET NULL on delete, so a hook running after the delete
    # would match zero rows. Same transaction as the delete below.
    try:
        on_reference_unavailable(
            session,
            workspace_id=workspace_id,
            reference_id=rid,
            workspaces_dir=workspaces_dir,
        )
    except Exception:  # noqa: BLE001 — invalidation is best-effort
        logger.exception("claim-analysis invalidation failed (hard-delete)")

    # Uncited → hard-delete: chunks first (FK), then the row, then bytes.
    # NOTE: ReferenceChunk.reference_id is ondelete="CASCADE", so the DB would
    # also cascade the chunk deletes when the Reference row is deleted. The
    # explicit delete here keeps the ORM identity map consistent and makes the
    # FK ordering self-documenting.
    session.query(ReferenceChunk).filter(
        ReferenceChunk.reference_id == rid,
        ReferenceChunk.workspace_id == workspace_id,
    ).delete(synchronize_session=False)
    session.add(
        WorkspaceEvent(
            workspace_id=workspace_id,
            kind="reference.deleted",
            actor_id=actor_id,
            payload={"reference_id": str(rid), "outcome": "deleted"},
        )
    )
    session.delete(ref)
    session.commit()

    if storage is not None:
        # Best-effort byte cleanup AFTER the DB commit (matches the ingest
        # rollback ordering: the DB row is gone before the file is removed).
        storage.delete_one(workspace_id=workspace_id, reference_id=rid)

    return "deleted"


def _is_cited(repo_dir: Path, reference_id: uuid.UUID) -> bool:
    """True if any evidence row in citations.json references this id.

    - Missing file → False (not cited; safe to delete).
    - Unreadable file (OSError, e.g. permission denied, directory in place of
      file) → True (fail-closed): cannot confirm the reference is uncited, so
      must NOT hard-delete on uncertainty.
    - Ledger present but unparseable (ValueError from load_ledger) → True
      (fail-closed): a corrupt ledger might reference this id; do not
      hard-delete on uncertainty.
    - Ledger present and parseable, no row matches reference_id → False.
    """
    citations_path = repo_dir / CITATIONS_FILENAME
    if not citations_path.exists():
        return False
    try:
        text = citations_path.read_text(encoding="utf-8")
        ledger = load_ledger(text)
    except (ValueError, OSError):
        # Fail closed: an unreadable (OSError) or corrupt (ValueError) ledger
        # might reference this id — do not hard-delete on uncertainty.
        return True
    target = str(reference_id)
    return any(row.get("reference_id") == target for row in ledger["evidence"])
