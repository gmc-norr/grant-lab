"""The delete-restore-resume hole (outside review, 2026-08-24).

An actor admitted at generation G can resume after a delete AND a restore have
both committed. `soft_deleted_at` is NULL again by then, so a liveness-only
check waves it through. Only a generation comparison catches it.

Fixture provenance (resolved against the real conftest, not guessed):
  - `pg_session_factory` is defined in tests/conftest.py — a fresh, per-test
    sessionmaker against a migrated testcontainers Postgres. There is no
    `session_factory` fixture in this repo.
  - There is no `seeded_workspace` or `admin_user` fixture either. Every
    existing test that exercises `restore_workspace_db` (e.g.
    tests/test_workspace_restore_service.py) builds its own workspace via the
    `make_user` / `make_workspace` helpers from
    tests.proposal_assist.conftest, inside a `with pg_session_factory() as s`
    block, and reuses the created owner as the admin_id — there is no
    ownership/role check at this DB-helper layer, only at the route. This test
    follows that same convention.

Both cases below are expected to PASS on the unmodified tree — this is a
characterization test, not red-green TDD. Case A passes because the hole is
real today. Case B passes because Primitive B (acquire_commit_mutex_at_generation,
in call_manager.proposal_assist.fencing) already works correctly; it simply has
no callers yet. Case A is what makes this test honest — without it, Case B
could pass for the wrong reason (e.g. a workspace fixture that never actually
gets restored).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from call_manager.db.models import Workspace
from call_manager.proposal_assist.drafting.locking import acquire_commit_mutex_live
from call_manager.proposal_assist.fencing import (
    WorkspaceGenerationStale,
    acquire_commit_mutex_at_generation,
)
from call_manager.workspaces import restore_workspace_db, soft_delete_workspace_db
from tests.proposal_assist.conftest import make_user, make_workspace


def _generation(session, ws_id: uuid.UUID) -> int:
    return session.execute(
        select(Workspace.lifecycle_generation).where(Workspace.id == ws_id)
    ).scalar_one()


def _seed_workspace(pg_session_factory) -> tuple[uuid.UUID, int]:
    """Seed a real Workspace row (lifecycle_generation defaults to 0 — a real
    generation, not a sentinel for "none yet") and return (workspace_id,
    admin_id). The admin reuses the workspace's own owner, exactly as
    tests/test_workspace_restore_service.py does: restore_workspace_db takes
    admin_id purely for the audit event at this layer, with no ownership
    check of its own."""
    with pg_session_factory() as s:
        owner = make_user(s)
        ws = make_workspace(s, owner=owner)
        s.commit()
        return ws.id, owner.id


def test_live_primitive_admits_the_stale_writer_after_restore(pg_session_factory):
    """Case A: the hole, demonstrated. This is why the slice exists."""
    ws_id, admin_id = _seed_workspace(pg_session_factory)
    with pg_session_factory() as s:
        admitted_at = _generation(s, ws_id)

    with pg_session_factory() as s:
        soft_delete_workspace_db(s, workspace_id=ws_id)
    with pg_session_factory() as s:
        restore_workspace_db(s, workspace_id=ws_id, admin_id=admin_id)

    with pg_session_factory() as s:
        assert _generation(s, ws_id) != admitted_at, (
            "delete+restore must move the generation, or this test proves nothing"
        )
        # The stale actor wakes up. The trusting primitive lets it write.
        acquire_commit_mutex_live(s, ws_id)  # does NOT raise — that is the hole


def test_generation_primitive_refuses_the_stale_writer_after_restore(pg_session_factory):
    """Case B: the fix. Same interleaving, Primitive B, refused."""
    ws_id, admin_id = _seed_workspace(pg_session_factory)
    with pg_session_factory() as s:
        admitted_at = _generation(s, ws_id)

    with pg_session_factory() as s:
        soft_delete_workspace_db(s, workspace_id=ws_id)
    with pg_session_factory() as s:
        restore_workspace_db(s, workspace_id=ws_id, admin_id=admin_id)

    with pg_session_factory() as s, pytest.raises(WorkspaceGenerationStale):
        acquire_commit_mutex_at_generation(s, ws_id, generation=admitted_at)
