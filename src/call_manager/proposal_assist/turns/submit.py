"""Atomically enqueue a user turn for an agent workspace.

This is the entry point B6's POST /workspaces/<id>/turns (now B7's
multipart-aware variant) calls. It writes one chat_turns row
(role='user'), one turn_jobs row (status='queued',
submitted_by_user_id=...), and one workspace_events audit row
(kind='cloud_turn_sent', phase='queued') in the caller's
session/transaction. Caller commits.

The audit row is written BEFORE dispatch so a crash between submit and
finalize doesn't lose the compliance trail. The processor writes a
second event (cloud_turn_completed or cloud_turn_failed) with telemetry
once the turn finishes; the cancel route writes cloud_turn_cancelled
if the user aborts a still-queued turn.

Cap enforcement: pre-write, calls check_daily_token_cap. If exceeded,
raises TokenCapExceeded — caller (typically the web layer) catches this
and returns 429 to the client. The session's flushed-but-uncommitted
state is rolled back by the caller (or by the request-scope teardown).

B7 attachment branch: when ``attachment_ids`` is non-empty, this
function:

  * Caps at 5 images per turn (combined uploads + references).
  * Resolves each attachment_id to an Attachment row WHERE
    ``workspace_id = workspace_id`` — cross-workspace references raise
    ``ValueError``.
  * Re-probes from disk if ``tokens_estimate IS NULL`` (covers the T2→T3
    migration window and any pre-B7 rows). Persists the result onto the
    row so subsequent submits are cheap. Missing file on disk →
    ``ValueError`` (NEVER silently zero — that would let an attacker
    bypass the cap via NULL rows).
  * Pre-counts ``tokens_in`` = sum(att.tokens_estimate) + len(text) // 4
    (text estimate matches the compaction heuristic in
    ``turns/compaction.py``) and writes it on the chat_turns row so the
    next concurrent submit's cap query sees the queued vision cost.

B8 will layer the user-facing "you hit the cap" surfacing on top of the
TokenCapExceeded path.
"""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.config import Settings
from call_manager.db.models import (
    Attachment,
    ChatTurn,
    Reference,
    TurnJob,
    Workspace,
    WorkspaceEvent,
)
from call_manager.proposal_assist.attachments.storage import AttachmentStorage
from call_manager.proposal_assist.attachments.tokens import (
    estimate_image_tokens_from_bytes,
)
from call_manager.proposal_assist.drafting.locking import acquire_commit_mutex
from call_manager.proposal_assist.turns.caps import (
    GlobalTokenCapExceeded,
    TokenCapExceeded,
    check_daily_token_cap,
    check_global_daily_token_cap,
)

MAX_TURN_INCLUSIONS = 5
"""Combined per-turn ceiling on attachments + references. Imported by
web/workspaces.py so the web-layer pre-check and the submit-layer
authoritative check use one constant — drift in either direction would
cause a UX/contract mismatch.
"""

REFERENCE_TEXT_BUDGET_CHARS = 50_000
"""Per-reference truncation budget for content injection. The resolver
caps each reference's text at this length, so the cap pre-count uses
``min(text_chars_total, REFERENCE_TEXT_BUDGET_CHARS)`` to match — over-
counting would refuse legitimately small turns; under-counting would
let cap-bypass attacks through. Mirrored in
``call_manager.proposal_assist.attachments.resolver`` (duplicated to
avoid a cyclic import; keep in sync)."""


def _build_user_content(
    *,
    content_text: str,
    attachment_ids: list[uuid.UUID] | None,
    reference_ids: list[uuid.UUID] | None = None,
    selection_block: dict[str, Any] | None = None,
) -> dict[str, Any] | list[dict[str, Any]]:
    """Shape the ``ChatTurn.content`` payload.

    Text-only submits keep the legacy dict shape so existing renderers
    and adapters don't have to special-case the common path. When
    either ``attachment_ids``, ``reference_ids``, or ``selection_block``
    is non-empty/non-None, we emit a list of blocks: the user text first,
    then the selection block (if any), then image blocks (one per
    attachment), then reference blocks (one per reference).
    """
    if not attachment_ids and not reference_ids and selection_block is None:
        return {"type": "text", "text": content_text}
    blocks: list[dict[str, Any]] = [{"type": "text", "text": content_text}]
    if selection_block is not None:
        blocks.append(selection_block)
    for aid in attachment_ids or []:
        blocks.append({"type": "image", "attachment_id": str(aid)})
    for rid in reference_ids or []:
        blocks.append({"type": "reference", "reference_id": str(rid)})
    return blocks


def _reprobe_null_token_estimates(
    *,
    session: Session,
    attachments: list[Attachment],
    workspace_id: uuid.UUID,
    workspaces_dir: Path,
) -> None:
    """Re-probe and persist tokens_estimate/width_px/height_px for any
    attachment row with NULL tokens_estimate.

    The Workspace FOR UPDATE lock held by the surrounding submit ensures
    no concurrent submit on the same workspace races this write. If the
    backing file is missing on disk, raise ValueError — the caller
    surfaces 400. NEVER silently default to zero: that would let an
    attacker bypass the daily-token cap by uploading via the T2 panel
    in the migration window before T3 backfilled dims.
    """
    storage = AttachmentStorage(root=workspaces_dir)
    needs_flush = False
    for att in attachments:
        if att.tokens_estimate is not None:
            continue
        suffix = Path(att.storage_path).suffix.lower()
        try:
            path = storage.path_for(
                workspace_id=workspace_id,
                attachment_id=att.id,
                suffix=suffix,
            )
        except ValueError as e:
            # Disallowed suffix snuck into the DB somehow — treat as
            # missing for the user-facing error path.
            raise ValueError(
                f"attachment {att.id} dimensions unknown and file missing — re-upload required"
            ) from e
        if not path.exists():
            raise ValueError(
                f"attachment {att.id} dimensions unknown and file missing — re-upload required"
            )
        data = path.read_bytes()
        # estimate_image_tokens_from_bytes re-probes via Pillow, so we
        # also derive width/height from the same code path to keep the
        # three columns mutually consistent.
        try:
            with Image.open(io.BytesIO(data)) as img:
                w, h = img.size
        except (UnidentifiedImageError, OSError) as e:
            raise ValueError(
                f"attachment {att.id} on-disk file is not a recognised image: {e}"
            ) from e
        est = estimate_image_tokens_from_bytes(data)
        att.tokens_estimate = est
        att.width_px = w
        att.height_px = h
        needs_flush = True
    if needs_flush:
        session.flush()


def submit_user_turn(
    *,
    session: Session,
    workspace_id: uuid.UUID,
    content_text: str,
    attachment_ids: list[uuid.UUID] | None = None,
    reference_ids: list[uuid.UUID] | None = None,
    submitted_by_user_id: int | None,
    settings: Settings,
    tokens_in: int | None = None,
    workspaces_dir: Path | None = None,
    selection_block: dict[str, Any] | None = None,
) -> TurnJob:
    """Insert chat_turns(role='user') + turn_jobs(status='queued') +
    workspace_events(cloud_turn_sent, phase='queued') atomically.
    Returns the TurnJob (not yet committed).

    ``content_text``: the user-typed message body. Stored on the
    chat_turns row as ``{"type":"text","text":content_text}`` when no
    attachments accompany the turn, or as the first element of a
    content-block list when one or more ``attachment_ids`` are present
    (matching the shape the resolver/processor expect).

    ``attachment_ids``: optional list of pre-existing
    :class:`~call_manager.db.models.Attachment` row ids to attach to
    this turn (B7). Each id is validated to belong to ``workspace_id``;
    cross-workspace references raise ``ValueError``. Combined inclusions
    (``attachment_ids`` + ``reference_ids``) are capped at
    ``MAX_TURN_INCLUSIONS`` per turn; over-cap raises ``ValueError``.
    Rows with NULL ``tokens_estimate`` trigger a disk re-probe via
    ``workspaces_dir`` — missing file ⇒ ``ValueError``.

    ``workspaces_dir``: filesystem root for the attachments tree
    (``<dir>/<wsid>/attachments/<aid>.<ext>``). Defaults to
    ``Path(settings.workspaces_dir)`` if not supplied — explicit kwarg
    is provided for tests and for callers that already constructed a
    storage helper.

    ``tokens_in``: when supplied explicitly, overrides the computed
    estimate. Callers (e.g. the concurrent-cap regression test) use
    this to pin the input budget without owning an attachment row.
    When omitted AND attachments are present, the function computes
    ``sum(att.tokens_estimate) + len(content_text) // 4`` and writes
    that value on the chat_turns row so the next concurrent submit's
    cap query sees the queued vision cost.

    Concurrency: takes a ``SELECT ... FOR UPDATE`` row lock on the
    workspaces row before reading today's token sums. The lock
    serializes per-workspace submits and is transaction-scoped
    (released on commit/rollback) — orthogonal to the session-scoped
    advisory lock that the drafting subprocess holds.

    Before B7 the lock was defense-in-depth for text-only traffic
    (cap query sums ``ChatTurn.tokens_in``, which the processor fills
    for assistant turns AFTER the LLM responds). With B7 attachment
    submits writing pre-counted ``tokens_in`` on the user-turn row at
    submit time, the lock now genuinely prevents two submits from
    both passing a cap they should not both pass.
    """
    # Spec 2026-06-10 §1: turn enqueue participates in the per-workspace commit
    # mutex so a user save's "no queued/running turn" check is authoritative —
    # either this enqueue lands first (save sees it → 409) or the save's
    # transaction commits first (turn starts strictly after the user's commit).
    acquire_commit_mutex(session, workspace_id)

    # Lock the workspace row for the duration of this transaction AND re-check
    # soft_deleted_at under the lock. submit and soft_delete serialize on this
    # row (submit's SELECT FOR UPDATE conflicts with soft_delete's UPDATE), so
    # re-reading here catches the post-deletion ordering: a None result means
    # the workspace is missing OR soft-deleted → reject, so no turn can be
    # enqueued (and orphan its cloud_turn_sent) after deletion.
    locked = session.execute(
        select(Workspace.id)
        .where(Workspace.id == workspace_id, Workspace.soft_deleted_at.is_(None))
        .with_for_update()
    ).scalar_one_or_none()
    if locked is None:
        raise ValueError(f"workspace {workspace_id} does not exist or was deleted")

    # Early input validation: empty submits would otherwise reach the
    # resolver and surface as the confusing
    # "attachment_resolution_failed: text block missing text" error.
    # Reject at the entry boundary with a clearer message.
    if not content_text and not attachment_ids:
        raise ValueError("turn must have text or attachments")
    if not content_text and attachment_ids:
        raise ValueError("content_text required when attachments are present")

    # ---- Combined inclusions cap (attachments + references) ----------
    n_inclusions = len(attachment_ids or []) + len(reference_ids or [])
    if n_inclusions > MAX_TURN_INCLUSIONS:
        raise ValueError(f"too many inclusions: {n_inclusions} > {MAX_TURN_INCLUSIONS}")

    # ---- B7 attachment resolution + token pre-count -----------------
    attachments: list[Attachment] = []
    if attachment_ids:
        # Resolve all rows in one query, then re-order by request order
        # so the rendered chat turn shows the user's chosen sequence.
        requested = list(attachment_ids)
        rows = list(
            session.scalars(
                select(Attachment).where(
                    Attachment.id.in_(requested),
                    Attachment.workspace_id == workspace_id,
                )
            )
        )
        by_id = {a.id: a for a in rows}
        for aid in requested:
            att = by_id.get(aid)
            if att is None:
                # Either no row at all, or the row exists in a different
                # workspace — both indistinguishable to the caller for
                # IDOR-leak reasons. Plan spec'd the cross-workspace
                # phrasing explicitly so callers can map it to 400.
                raise ValueError(f"attachment {aid} does not belong to workspace {workspace_id}")
            attachments.append(att)

        # NULL tokens_estimate re-probe (closes the cap-bypass via the
        # T2→T3 migration window).
        ws_dir = workspaces_dir or Path(settings.workspaces_dir)
        _reprobe_null_token_estimates(
            session=session,
            attachments=attachments,
            workspace_id=workspace_id,
            workspaces_dir=ws_dir,
        )

    # ---- Slice 4 reference resolution + IDOR validation --------------
    # Runs inside the workspace FOR UPDATE lock window so a concurrent
    # mutation can't slip a not-yet-done reference past us. Validates
    # existence + workspace membership + text_status='done' (the
    # contract for "the resolver can find text.txt"). Single query +
    # request-order iteration mirrors the attachment block above.
    references: list[Reference] = []
    if reference_ids:
        # Scope the query to this workspace (defense-in-depth, matching
        # the attachment query above). The Python-side
        # ``ref.workspace_id != workspace_id`` check below still stands
        # so cross-workspace ids surface the IDOR-safe "does not belong"
        # error instead of silently dropping to "not found".
        ref_rows = list(
            session.scalars(
                select(Reference).where(
                    Reference.id.in_(reference_ids),
                    Reference.workspace_id == workspace_id,
                )
            )
        )
        ref_by_id = {r.id: r for r in ref_rows}
        for rid in reference_ids:
            ref = ref_by_id.get(rid)
            if ref is None:
                raise ValueError(f"reference {rid} does not belong to workspace {workspace_id}")
            if ref.workspace_id != workspace_id:
                # Cross-workspace: indistinguishable from "not found"
                # to avoid leaking ref ids across tenants.
                raise ValueError(f"reference {rid} does not belong to workspace {workspace_id}")
            if ref.text_status != "done":
                raise ValueError(f"reference {rid} is not ready (text_status={ref.text_status!r})")
            references.append(ref)

    # A selection block injects server-side text (the anchored span, up to
    # MAX_SELECTION_CODEPOINTS, plus its heading) into the prompt beyond what
    # the user typed. Count it so a large selection can't slip past the daily
    # cap; the small fixed <selection> wrapper is rounded away by ``// 4``.
    sel_chars = (
        len(selection_block.get("anchor") or "") + len(selection_block.get("heading") or "")
        if selection_block
        else 0
    )

    if tokens_in is None and (attachments or references or selection_block):
        # ``text_chars_total`` is NULL for the extracted-but-not-counted
        # edge case — treat as 0 (defensive: the resolver still finds
        # text.txt; the SDK's TurnDoneEvent corrects the running total
        # post-call). Cap the per-ref contribution at the resolver's
        # truncation budget so we don't overestimate large refs.
        ref_chars = sum(
            min((r.text_chars_total or 0), REFERENCE_TEXT_BUDGET_CHARS) for r in references
        )
        tokens_in = (
            sum(att.tokens_estimate or 0 for att in attachments)
            + (ref_chars // 4)
            + (sel_chars // 4)
            + (len(content_text) // 4)
        )

    # Global cap check (org-wide guardrail). Read-only — no global
    # advisory lock. See check_global_daily_token_cap docstring.
    global_cap = check_global_daily_token_cap(session, settings=settings)
    if not global_cap.allowed:
        raise GlobalTokenCapExceeded(
            used_in=global_cap.used_in,
            used_out=global_cap.used_out,
            cap_in=global_cap.cap_in,
            cap_out=global_cap.cap_out,
            reason=global_cap.reason or "cap_exceeded",
        )

    cap = check_daily_token_cap(session, workspace_id, settings=settings)
    if not cap.allowed:
        raise TokenCapExceeded(
            workspace_id,
            used_in=cap.used_in,
            used_out=cap.used_out,
            cap_in=cap.cap_in,
            cap_out=cap.cap_out,
            reason=cap.reason or "cap_exceeded",
        )

    content = _build_user_content(
        content_text=content_text,
        attachment_ids=attachment_ids,
        reference_ids=reference_ids,
        selection_block=selection_block,
    )

    user_turn = ChatTurn(
        workspace_id=workspace_id,
        role="user",
        content=content,
        tokens_in=tokens_in,
    )
    session.add(user_turn)
    session.flush()  # so user_turn.id is populated for FK below

    job = TurnJob(
        workspace_id=workspace_id,
        user_turn_id=user_turn.id,
        submitted_by_user_id=submitted_by_user_id,
        status="queued",
        queued_at=datetime.now(UTC),
    )
    session.add(job)
    session.flush()

    # Audit-at-submit: survives a crash between submit and finalize.
    # The processor writes a separate cloud_turn_{completed,failed}
    # event with telemetry when the turn finishes.
    session.add(
        WorkspaceEvent(
            workspace_id=workspace_id,
            kind="cloud_turn_sent",
            actor_id=submitted_by_user_id,
            payload={
                "turn_job_id": str(job.id),
                "phase": "queued",
            },
        )
    )
    session.flush()
    return job
