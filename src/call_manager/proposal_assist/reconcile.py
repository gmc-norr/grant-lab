"""Workspace git ↔ DB reconciliation.

Spec line 248–268. The filesystem (git) and Postgres are not in the
same transaction, so commits can land without their matching
artifact_versions row. Reconciliation backfills.

Triggers:
  - on workspace open (controller.launch invokes this — Task 8).
  - on-demand from /admin (B6/B8 will wire that).

Forward-only and never destructive: git commits are never undone.
Orphan-commit case → backfill an artifact_versions row with the
commit's actual timestamp (Codex C3: `%ct` from git log).
Orphan-row case (sha in DB, no git commit) → impossible under the
write order (git first); if it appears, mark created_by='unrecoverable'
and alert. Out of scope here.
"""

from __future__ import annotations

import logging
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import insert, literal, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from call_manager.db.models import ArtifactVersion, Reference, Workspace, WorkspaceEvent
from call_manager.proposal_assist.citations.ledger import load_ledger
from call_manager.proposal_assist.fencing import acquire_shared_lease, live_at_generation
from call_manager.proposal_assist.ports.artifacts import CITATIONS_FILENAME

logger = logging.getLogger(__name__)


def reconcile_workspace_git_drift(
    session_factory: sessionmaker,
    workspace_id: uuid.UUID,
    repo_dir: Path,
) -> int:
    """Backfill artifact_versions rows for git commits that don't have
    one. Returns the count of rows inserted.

    Codex C3: preserves commit timestamps (%ct = committer Unix time)
    so backfilled rows' created_at matches when the commit actually
    landed — not when reconciliation ran. Spec line 258 mandates this.
    """
    if not (repo_dir / ".git").is_dir():
        return 0
    try:
        proc = subprocess.run(
            ["git", "log", "--pretty=format:%H%x00%ct%x00%s"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        logger.warning(
            "reconcile_workspace_git_drift: git log failed for %s: %s",
            workspace_id,
            e.stderr,
        )
        return 0

    git_lines = [line for line in proc.stdout.strip().split("\n") if line]
    if not git_lines:
        return 0
    # Each line: <sha>\x00<committer_unix_ts>\x00<subject>
    git_commits: list[tuple[str, int, str]] = []
    for line in git_lines:
        sha, ts_str, message = line.split("\x00", 2)
        git_commits.append((sha, int(ts_str), message))

    with session_factory() as s:
        # Primitive C FIRST. Shared locks do not conflict with each other, so
        # concurrent reconciles stay concurrent, but a delete or restore needs
        # this key EXCLUSIVELY — so it either waits behind us or wins first and
        # leaves the predicate below to refuse.
        acquire_shared_lease(s, workspace_id)
        # Read the generation UNDER the lease: the belief this effect carries is
        # the git log read a few lines above, not an HTTP authorization
        # boundary, so this is the right place to form it.
        generation = s.scalar(
            select(Workspace.lifecycle_generation).where(Workspace.id == workspace_id)
        )
        if generation is None:
            return 0  # workspace row is gone entirely (hard delete)
        existing_shas = set(
            s.scalars(
                select(ArtifactVersion.commit_sha).where(
                    ArtifactVersion.workspace_id == workspace_id
                )
            ).all()
        )
        backfilled = 0
        for sha, ts, message in git_commits:
            if sha in existing_shas:
                continue
            # INSERT ... SELECT ... WHERE, never INSERT ... VALUES after a check.
            # An INSERT has no target row, so there is no READ COMMITTED re-check
            # to mask a missing fence; check-then-insert is the exact race
            # reproduced on PostgreSQL 16 (see port_impl/effects.py:258-268).
            #
            # THE LOAD-BEARING TERMS ARE THE LEASE ABOVE AND THE LIVENESS CLAUSE
            # HERE — NOT the generation. Under the lease the generation cannot
            # move, so the equality term can never fail; it is kept for
            # uniformity with every other mechanism-1 site. If you are tempted
            # to simplify this, drop the generation term, NEVER the liveness
            # term and NEVER the lease.
            landed = s.execute(
                insert(ArtifactVersion)
                .from_select(
                    [
                        "id",
                        "workspace_id",
                        "commit_sha",
                        "triggering_turn_id",
                        "created_by",
                        "message",
                        "created_at",
                    ],
                    select(
                        literal(uuid.uuid4()),
                        literal(workspace_id),
                        literal(sha),
                        literal(None),
                        literal("reconciled"),
                        literal(message),
                        literal(datetime.fromtimestamp(ts, tz=UTC)),
                    ).where(live_at_generation(workspace_id, generation)),
                )
                .returning(ArtifactVersion.id)
            ).first()
            if landed is None:
                # Under the lease the generation cannot move, so if ANY insert is
                # refused the first one was — nothing has landed yet. Roll back
                # anyway rather than committing a partial batch, and leave every
                # orphan for a later run.
                logger.info(
                    "reconcile_workspace_git_drift: refused for %s — workspace "
                    "not live at generation %s; orphan commits left for a later run",
                    workspace_id,
                    generation,
                )
                s.rollback()
                return 0
            backfilled += 1
        if backfilled > 0:
            # Safe as a plain add: same transaction, still holding the lease, and
            # the predicate above already passed — so the workspace is provably
            # live at `generation` right now.
            s.add(
                WorkspaceEvent(
                    workspace_id=workspace_id,
                    kind="git_drift_reconciled",
                    payload={"backfilled_count": backfilled},
                )
            )
        s.commit()
    return backfilled


def reconcile_workspace_citekeys(
    session_factory: sessionmaker,
    workspace_id: uuid.UUID,
    repo_dir: Path,
) -> int:
    """Repair the references.citekey DB cache from the citations.json ledger.

    Spec contract (mirrors reconcile_workspace_git_drift): the git ledger is
    canonical and the references.citekey column is a DERIVED best-effort cache.
    A best-effort cache write inside insert/rename can fail after the atomic git
    commit lands, leaving references.citekey stale or NULL. This backfills it.

    Forward-only, idempotent, and never destructive: it sets a NULL or
    NON-MATCHING cache value to the ledger's citekey (the ledger is canonical,
    so a cache value that disagrees is overwritten), never the other way round,
    and never deletes a row. It respects the partial unique index on
    (workspace_id, citekey): if a DIFFERENT reference already holds the target
    citekey, that's a genuine conflict the index would reject, so we log and
    skip it (human / Slice-3 resolution) rather than crash. Never raises for
    expected failure modes — logs and continues. Returns the number of
    references.citekey values repaired.
    """
    citations_path = repo_dir / CITATIONS_FILENAME
    if not citations_path.exists():
        return 0
    try:
        ledger = load_ledger(citations_path.read_text())
    except (ValueError, OSError) as exc:
        logger.warning(
            "reconcile_workspace_citekeys: could not load %s for %s: %s",
            CITATIONS_FILENAME,
            workspace_id,
            exc,
        )
        return 0

    # Build reference_id -> citekey from the ledger. A reference may appear in
    # many rows; they must all share one citekey. A reference_id mapping to
    # MULTIPLE distinct citekeys is corruption — skip it (don't guess).
    desired: dict[str, str] = {}
    conflicting: set[str] = set()
    for row in ledger["evidence"]:
        if not isinstance(row, dict):
            continue
        ref_id = row.get("reference_id")
        citekey = row.get("citekey")
        if not ref_id or not citekey:
            continue
        prev = desired.get(ref_id)
        if prev is not None and prev != citekey:
            conflicting.add(ref_id)
        else:
            desired[ref_id] = citekey
    for ref_id in conflicting:
        logger.warning(
            "reconcile_workspace_citekeys: reference %s maps to multiple distinct "
            "citekeys in the ledger for workspace %s — skipping (corruption)",
            ref_id,
            workspace_id,
        )
        desired.pop(ref_id, None)

    if not desired:
        return 0

    repaired = 0
    with session_factory() as s:
        # Primitive C first — see reconcile_workspace_git_drift for the reasoning.
        acquire_shared_lease(s, workspace_id)
        generation = s.scalar(
            select(Workspace.lifecycle_generation).where(Workspace.id == workspace_id)
        )
        if generation is None:
            return 0
        for ref_id_str, ledger_citekey in desired.items():
            try:
                ref_uuid = uuid.UUID(str(ref_id_str))
            except (ValueError, AttributeError, TypeError):
                continue
            ref = s.get(Reference, ref_uuid)
            if ref is None or ref.workspace_id != workspace_id:
                continue
            if ref.citekey == ledger_citekey:
                continue
            # Respect the partial unique index: if another reference in this
            # workspace already holds the target citekey, the write would be
            # rejected. Skip — a genuine conflict needs human/Slice-3 resolution.
            holder = s.scalar(
                select(Reference.id).where(
                    Reference.workspace_id == workspace_id,
                    Reference.citekey == ledger_citekey,
                    Reference.id != ref_uuid,
                )
            )
            if holder is not None:
                logger.warning(
                    "reconcile_workspace_citekeys: citekey %r already held by "
                    "reference %s in workspace %s — skipping repair of reference %s",
                    ledger_citekey,
                    holder,
                    workspace_id,
                    ref_uuid,
                )
                continue
            try:
                # The fence and the write are ONE statement. THE LOAD-BEARING TERMS
                # ARE THE LEASE AND THE LIVENESS CLAUSE — see the note in
                # reconcile_workspace_git_drift.
                #
                # The savepoint stays. Its documented reason still holds: a
                # concurrent write racing the holder check above can still raise
                # IntegrityError, and only this one row should roll back. The
                # rowcount == 0 branch below is the NEW fence path and is a
                # different case from that IntegrityError.
                with s.begin_nested():
                    result = s.execute(
                        update(Reference)
                        .where(
                            Reference.id == ref_uuid,
                            Reference.workspace_id == workspace_id,
                            live_at_generation(workspace_id, generation),
                        )
                        .values(citekey=ledger_citekey)
                    )
                if result.rowcount == 0:
                    logger.info(
                        "reconcile_workspace_citekeys: refused for %s — workspace "
                        "not live at generation %s; leaving citekey cache as-is",
                        workspace_id,
                        generation,
                    )
                    s.rollback()
                    return 0
                repaired += 1
            except IntegrityError:
                logger.warning(
                    "reconcile_workspace_citekeys: integrity error repairing "
                    "reference %s -> %r in workspace %s — skipping",
                    ref_uuid,
                    ledger_citekey,
                    workspace_id,
                    exc_info=True,
                )
        s.commit()
    return repaired
