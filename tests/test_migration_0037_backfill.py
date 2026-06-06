"""Tests for migration 0037: lazy backfill of references.original_sha256.

Two test groups:
1. Function-level test — verifies backfill_original_sha256() logic directly
   against the real DB (pg_session_factory + tmp_path).
2. Migration round-trip (light) — verifies 0037 is reachable, and that
   downgrade(0036) + upgrade(head) completes cleanly.

pg_url / migrated_engine are the SHARED session-scoped container; the
round-trip test leaves schema at head in a finally block.
"""

from __future__ import annotations

import hashlib
import uuid

from alembic.config import Config
from sqlalchemy import select

from alembic import command
from call_manager.db.models import Reference
from tests.proposal_assist.conftest import make_user, make_workspace

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_ref(session, workspace, *, suffix: str = ".pdf") -> Reference:
    ref = Reference(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        kind="paper",
        source_type="uploaded",
        filename=f"original{suffix}",
        storage_path=f"{workspace.id}/references/{uuid.uuid4()}/original{suffix}",
        mime_type="application/pdf",
        bytes_total=512,
        uploaded_by=workspace.owner_id,
        original_sha256=None,  # explicitly pre-0036
    )
    session.add(ref)
    session.flush()
    return ref


# ---------------------------------------------------------------------------
# 1. Function tests (primary)
# ---------------------------------------------------------------------------


def test_backfill_fills_present_skips_absent(pg_session_factory, tmp_path):
    """Ref A has its file on disk → gets hashed. Ref B has no file → stays NULL."""
    from call_manager.proposal_assist.references.backfill import backfill_original_sha256

    file_bytes = b"hello world pdf content"
    expected_hash = hashlib.sha256(file_bytes).hexdigest()

    with pg_session_factory() as session:
        owner = make_user(session)
        ws = make_workspace(session, owner=owner)
        session.commit()

        ref_a = _make_ref(session, ws, suffix=".pdf")
        ref_b = _make_ref(session, ws, suffix=".pdf")
        session.commit()

        ref_a_id = ref_a.id
        ref_a_path = ref_a.storage_path
        ref_b_id = ref_b.id

        # Write file for ref_a only
        full_path = tmp_path / ref_a_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_bytes(file_bytes)

        count = backfill_original_sha256(session, tmp_path)
        session.commit()

    assert count == 1

    with pg_session_factory() as session:
        a = session.scalar(select(Reference).where(Reference.id == ref_a_id))
        b = session.scalar(select(Reference).where(Reference.id == ref_b_id))

    assert a is not None
    assert a.original_sha256 == expected_hash, (
        f"expected {expected_hash!r}, got {a.original_sha256!r}"
    )
    assert b is not None
    assert b.original_sha256 is None


def test_backfill_idempotent(pg_session_factory, tmp_path):
    """Second call on already-filled rows returns 0 and doesn't alter the hash."""
    from call_manager.proposal_assist.references.backfill import backfill_original_sha256

    file_bytes = b"idempotent test content"
    expected_hash = hashlib.sha256(file_bytes).hexdigest()

    with pg_session_factory() as session:
        owner = make_user(session)
        ws = make_workspace(session, owner=owner)
        session.commit()

        ref = _make_ref(session, ws)
        session.commit()

        ref_id = ref.id
        ref_path = ref.storage_path

        full_path = tmp_path / ref_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_bytes(file_bytes)

        # First call
        count1 = backfill_original_sha256(session, tmp_path)
        session.commit()

    assert count1 == 1

    with pg_session_factory() as session:
        a = session.scalar(select(Reference).where(Reference.id == ref_id))
        assert a is not None
        assert a.original_sha256 == expected_hash

        # Second call — row is no longer NULL, so nothing to do
        count2 = backfill_original_sha256(session, tmp_path)
        session.commit()

    assert count2 == 0

    # Hash unchanged
    with pg_session_factory() as session:
        a = session.scalar(select(Reference).where(Reference.id == ref_id))
        assert a.original_sha256 == expected_hash


def test_backfill_returns_zero_when_nothing_to_do(pg_session_factory, tmp_path):
    """If there are no NULL rows, returns 0 immediately."""
    from call_manager.proposal_assist.references.backfill import backfill_original_sha256

    with pg_session_factory() as session:
        count = backfill_original_sha256(session, tmp_path)

    assert count == 0


def test_backfill_skips_unreadable_file(pg_session_factory, tmp_path, monkeypatch):
    """If reading a file raises an exception, the row is skipped and the
    function still returns normally (does NOT propagate the error)."""
    from call_manager.proposal_assist.references import backfill as backfill_mod
    from call_manager.proposal_assist.references.backfill import backfill_original_sha256

    with pg_session_factory() as session:
        owner = make_user(session)
        ws = make_workspace(session, owner=owner)
        session.commit()

        ref = _make_ref(session, ws)
        session.commit()

        ref_id = ref.id
        ref_path = ref.storage_path

        # Create the file so path.is_file() passes, but patch read_bytes to raise
        full_path = tmp_path / ref_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_bytes(b"content")

        def _boom(self):
            raise OSError("simulated read error")

        monkeypatch.setattr(backfill_mod.Path, "read_bytes", _boom)

        count = backfill_original_sha256(session, tmp_path)
        session.commit()

    # Error swallowed; row left NULL; count=0
    assert count == 0

    with pg_session_factory() as session:
        r = session.scalar(select(Reference).where(Reference.id == ref_id))
        assert r.original_sha256 is None


def test_backfill_skips_escaping_storage_path(pg_session_factory, tmp_path):
    """A Reference whose storage_path escapes references_root (e.g. via '../..')
    must be silently skipped — original_sha256 stays NULL, no exception raised,
    no file outside the root is read."""
    from call_manager.proposal_assist.references.backfill import backfill_original_sha256

    with pg_session_factory() as session:
        owner = make_user(session)
        ws = make_workspace(session, owner=owner)
        session.commit()

        # Build a Reference whose storage_path traverses above tmp_path.
        ref = Reference(
            id=uuid.uuid4(),
            workspace_id=ws.id,
            kind="paper",
            source_type="uploaded",
            filename="escape.pdf",
            storage_path="../../etc/passwd",  # path-traversal attempt
            mime_type="application/pdf",
            bytes_total=0,
            uploaded_by=ws.owner_id,
            original_sha256=None,
        )
        session.add(ref)
        session.commit()
        ref_id = ref.id

        count = backfill_original_sha256(session, tmp_path)
        session.commit()

    # The escaping path must be skipped — count stays 0, sha256 stays NULL.
    assert count == 0

    with pg_session_factory() as session:
        r = session.scalar(select(Reference).where(Reference.id == ref_id))
        assert r is not None
        assert r.original_sha256 is None


# ---------------------------------------------------------------------------
# 2. Migration round-trip (light)
# ---------------------------------------------------------------------------


def _cfg(pg_url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    return cfg


def test_0037_round_trip(migrated_engine, pg_url):
    """Downgrade to 0036 then upgrade back to head. No data ops — just schema."""
    from sqlalchemy import create_engine, inspect

    engine = create_engine(pg_url)
    try:
        # Confirm we're at head (0037 applied)
        with engine.connect() as conn:
            tables = inspect(conn).get_table_names()
            # 0036 tables still present
            assert "citation_llm_usage" in tables
            # original_sha256 column exists (added by 0036, kept by 0037)
            cols = {c["name"] for c in inspect(conn).get_columns("references")}
            assert "original_sha256" in cols

        # Downgrade to 0036 (0037 downgrade is a no-op)
        command.downgrade(_cfg(pg_url), "0036")
        with engine.connect() as conn:
            # schema unchanged after no-op downgrade
            cols = {c["name"] for c in inspect(conn).get_columns("references")}
            assert "original_sha256" in cols

        # Upgrade back to head
        command.upgrade(_cfg(pg_url), "head")
        with engine.connect() as conn:
            cols = {c["name"] for c in inspect(conn).get_columns("references")}
            assert "original_sha256" in cols
    finally:
        # Always restore head for subsequent tests in the shared container
        command.upgrade(_cfg(pg_url), "head")
        engine.dispose()
