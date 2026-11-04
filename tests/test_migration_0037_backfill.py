"""Migration 0037: best-effort backfill of references.original_sha256.

Two groups:
1. The frozen backfill, called directly against the real database with rows and
   files under tmp_path. It is loaded from the migration file, because that is
   where the code now lives: the package it was copied from left master on
   2026-11-03.
2. The migration end to end: downgrade to 0036, seed references that predate the
   backfill, write their files, upgrade to 0037, read the hashes back. This is the
   only test that can see a broken 0037. upgrade() logs and swallows any error in
   the data pass, so the suite's `alembic upgrade head` on an empty database
   passes whether the backfill works or not.

pg_url / migrated_engine are the SHARED session-scoped container; the end-to-end
test restores head in a finally block.
"""

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import create_engine, inspect, select, text

from alembic import command
from grant_lab.db.models.workspace import Reference
from tests.migrations.support import alembic_cfg, load_migration, make_workspace

M = load_migration("0037_backfill_reference_original_sha256.py")


def _make_ref(session, workspace, *, storage_path: str | None = None) -> Reference:
    ref = Reference(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        kind="paper",
        source_type="uploaded",
        filename="original.pdf",
        storage_path=storage_path or f"{workspace.id}/references/{uuid.uuid4()}/original.pdf",
        mime_type="application/pdf",
        bytes_total=512,
        uploaded_by=workspace.owner_id,
        original_sha256=None,  # explicitly pre-0036
    )
    session.add(ref)
    session.flush()
    return ref


def _sha_of(session_factory, ref_id):
    with session_factory() as s:
        return s.scalar(select(Reference.original_sha256).where(Reference.id == ref_id))


# ---------------------------------------------------------------------------
# 1. The frozen backfill
# ---------------------------------------------------------------------------


def test_backfill_fills_present_skips_absent(pg_session_factory, tmp_path):
    """Ref A has its file on disk -> hashed. Ref B has no file -> stays NULL."""
    file_bytes = b"hello world pdf content"

    with pg_session_factory() as session:
        ws = make_workspace(session)
        ref_a = _make_ref(session, ws)
        ref_b = _make_ref(session, ws)
        session.commit()
        full_path = tmp_path / ref_a.storage_path
        full_path.parent.mkdir(parents=True)
        full_path.write_bytes(file_bytes)

        count = M._backfill_original_sha256(session.connection(), tmp_path)
        session.commit()

    assert count == 1
    assert _sha_of(pg_session_factory, ref_a.id) == hashlib.sha256(file_bytes).hexdigest()
    assert _sha_of(pg_session_factory, ref_b.id) is None


def test_each_row_gets_the_hash_of_its_own_file(pg_session_factory, tmp_path):
    """Two files, two different hashes. A copy that hashed one file for every row,
    or mixed rows up, passes the single-file test above."""
    with pg_session_factory() as session:
        ws = make_workspace(session)
        refs = [_make_ref(session, ws), _make_ref(session, ws)]
        session.commit()
        for i, ref in enumerate(refs):
            p = tmp_path / ref.storage_path
            p.parent.mkdir(parents=True)
            p.write_bytes(f"paper number {i}".encode())

        assert M._backfill_original_sha256(session.connection(), tmp_path) == 2
        session.commit()

    for i, ref in enumerate(refs):
        expected = hashlib.sha256(f"paper number {i}".encode()).hexdigest()
        assert _sha_of(pg_session_factory, ref.id) == expected


def test_backfill_idempotent(pg_session_factory, tmp_path):
    """Second call on already-filled rows returns 0 and doesn't alter the hash."""
    file_bytes = b"idempotent test content"
    expected_hash = hashlib.sha256(file_bytes).hexdigest()

    with pg_session_factory() as session:
        ws = make_workspace(session)
        ref = _make_ref(session, ws)
        session.commit()
        full_path = tmp_path / ref.storage_path
        full_path.parent.mkdir(parents=True)
        full_path.write_bytes(file_bytes)

        count1 = M._backfill_original_sha256(session.connection(), tmp_path)
        session.commit()
    assert count1 == 1
    assert _sha_of(pg_session_factory, ref.id) == expected_hash

    full_path.write_bytes(b"the file changed since")  # must not be re-read
    with pg_session_factory() as session:
        count2 = M._backfill_original_sha256(session.connection(), tmp_path)
        session.commit()
    assert count2 == 0
    assert _sha_of(pg_session_factory, ref.id) == expected_hash


def test_backfill_returns_zero_when_nothing_to_do(pg_session_factory, tmp_path):
    with pg_session_factory() as session:
        assert M._backfill_original_sha256(session.connection(), tmp_path) == 0


def test_backfill_skips_unreadable_file(pg_session_factory, tmp_path, monkeypatch):
    """A read that raises skips the row; the function still returns normally."""
    with pg_session_factory() as session:
        ws = make_workspace(session)
        ref = _make_ref(session, ws)
        session.commit()
        full_path = tmp_path / ref.storage_path
        full_path.parent.mkdir(parents=True)
        full_path.write_bytes(b"content")  # exists, so is_file() passes

        def _boom(self):
            raise OSError("simulated read error")

        monkeypatch.setattr(M.Path, "read_bytes", _boom)
        count = M._backfill_original_sha256(session.connection(), tmp_path)
        session.commit()

    assert count == 0
    assert _sha_of(pg_session_factory, ref.id) is None


def test_backfill_skips_escaping_storage_path(pg_session_factory, tmp_path):
    """A storage_path that resolves outside the root is skipped and its file is
    never read. The file it points at EXISTS, so only the escape check can stop
    it (the version of this test before 2026-11-03 pointed at a missing file, and
    would have passed with the check deleted)."""
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside.pdf").write_bytes(b"not under the root")

    with pg_session_factory() as session:
        ws = make_workspace(session)
        ref = _make_ref(session, ws, storage_path="../outside.pdf")
        session.commit()
        count = M._backfill_original_sha256(session.connection(), root)
        session.commit()

    assert count == 0
    assert _sha_of(pg_session_factory, ref.id) is None


# ---------------------------------------------------------------------------
# 2. The migration end to end
# ---------------------------------------------------------------------------


def test_upgrade_0037_hashes_rows_that_predate_it(
    pg_url, pg_session_factory, tmp_path, monkeypatch
):
    """Downgrade to 0036, seed there, upgrade to 0037, read the hashes back.

    `pg_session_factory` is requested only for its teardown TRUNCATE, same as
    tests/test_migration_0064_claimed_at_generation.py.
    """
    cfg = alembic_cfg(pg_url)
    eng = create_engine(pg_url)
    root = tmp_path / "root"
    present_bytes = b"%PDF-1.4 a paper uploaded before 0036"
    try:
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "0036")
        try:
            with eng.begin() as c:
                uid = c.execute(
                    text("INSERT INTO users (email) VALUES ('m0037@umu.se') RETURNING id")
                ).scalar_one()
                wid = c.execute(
                    text(
                        "INSERT INTO workspaces (id, title, owner_id) "
                        "VALUES (gen_random_uuid(), 'W0037', :u) RETURNING id"
                    ),
                    {"u": uid},
                ).scalar_one()

                def ref(storage_path):
                    return c.execute(
                        text(
                            'INSERT INTO "references" (id, workspace_id, kind, source_type, '
                            "filename, storage_path, mime_type) VALUES (gen_random_uuid(), "
                            ":w, 'paper', 'uploaded', 'original.pdf', :p, 'application/pdf') "
                            "RETURNING id"
                        ),
                        {"w": wid, "p": storage_path},
                    ).scalar_one()

                present = ref(f"{wid}/references/a/original.pdf")
                absent = ref(f"{wid}/references/b/original.pdf")
                escaping = ref("../outside.pdf")
            target = root / str(wid) / "references" / "a" / "original.pdf"
            target.parent.mkdir(parents=True)
            target.write_bytes(present_bytes)
            (tmp_path / "outside.pdf").write_bytes(b"not under the root")
            monkeypatch.setenv("WORKSPACES_DIR", str(root))

            command.upgrade(cfg, "0037")

            with eng.connect() as c:
                got = dict(
                    c.execute(
                        text(
                            'SELECT id, original_sha256 FROM "references" WHERE workspace_id = :w'
                        ),
                        {"w": wid},
                    ).all()
                )
        finally:
            command.upgrade(cfg, "head")

        with eng.connect() as c:
            cols = {col["name"] for col in inspect(c).get_columns("references")}
        assert "original_sha256" in cols
    finally:
        eng.dispose()

    assert got == {
        present: hashlib.sha256(present_bytes).hexdigest(),
        absent: None,
        escaping: None,
    }
