"""Round-trip checks for migration 0040 (bibliography_settings).

pg_url is the SHARED session container; this test downgrades then upgrades,
so it MUST leave the schema at head for later tests — done in finally.

Tests:
- Round-trip head→0039→head: three new columns + CHECK exist at head, gone
  after downgrade to 0039.
- Model round-trip: Workspace.citation_style defaults to 'vancouver', accepts
  'author-year', rejects unknown values (IntegrityError). Reference.metadata_waiver
  defaults False, accepts True + reason, round-trips.
"""

from __future__ import annotations

import uuid

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError

from alembic import command

# ---------------------------------------------------------------------------
# Migration round-trip helpers
# ---------------------------------------------------------------------------


def _cfg(pg_url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    return cfg


def _columns(conn, table: str) -> set[str]:
    return {c["name"] for c in inspect(conn).get_columns(table)}


def _check_constraints(conn, table: str) -> set[str]:
    return {c["name"] for c in inspect(conn).get_check_constraints(table)}


# ---------------------------------------------------------------------------
# Migration round-trip
# ---------------------------------------------------------------------------


def test_0040_round_trip(migrated_engine, pg_url):
    engine = create_engine(pg_url)
    try:
        # At head: the three new columns exist in their respective tables.
        with engine.connect() as conn:
            ws_cols = _columns(conn, "workspaces")
            assert "citation_style" in ws_cols

            ref_cols = _columns(conn, "references")
            assert "metadata_waiver" in ref_cols
            assert "metadata_waiver_reason" in ref_cols

            # CHECK constraint must be present on workspaces.
            ws_checks = _check_constraints(conn, "workspaces")
            assert "ck_workspaces_citation_style" in ws_checks

        # Downgrade to 0039: the three columns must be gone.
        command.downgrade(_cfg(pg_url), "0039")
        with engine.connect() as conn:
            ws_cols = _columns(conn, "workspaces")
            assert "citation_style" not in ws_cols

            ref_cols = _columns(conn, "references")
            assert "metadata_waiver" not in ref_cols
            assert "metadata_waiver_reason" not in ref_cols

            ws_checks = _check_constraints(conn, "workspaces")
            assert "ck_workspaces_citation_style" not in ws_checks

    finally:
        # ALWAYS restore head, even if an assertion above failed — later tests
        # in this session reuse the shared container at head.
        command.upgrade(_cfg(pg_url), "head")
        engine.dispose()


# ---------------------------------------------------------------------------
# Model round-trip tests
# ---------------------------------------------------------------------------


def _make_user(session, email: str = "bib-owner@umu.se"):
    from grant_lab.db.models import User

    u = User(email=email)
    session.add(u)
    session.flush()
    return u


def _make_workspace(session, owner=None):
    from grant_lab.db.models import Workspace

    if owner is None:
        owner = _make_user(session)
    ws = Workspace(title="Bibliography test workspace", owner_id=owner.id)
    session.add(ws)
    session.flush()
    return ws


def _make_reference(session, workspace):
    from grant_lab.db.models import Reference

    ref = Reference(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        kind="paper",
        source_type="uploaded",
        filename="test.pdf",
        storage_path=f"{workspace.id}/references/bib-test/original.pdf",
        mime_type="application/pdf",
    )
    session.add(ref)
    session.flush()
    return ref


def test_citation_style_defaults_to_vancouver(pg_session_factory):
    """Workspace.citation_style server_default is 'vancouver'."""
    with pg_session_factory() as s:
        ws = _make_workspace(s)
        s.commit()
        ws_id = ws.id

    with pg_session_factory() as s:
        loaded = s.get(type(ws), ws_id)
        assert loaded is not None
        assert loaded.citation_style == "vancouver"


def test_citation_style_accepts_author_year(pg_session_factory):
    """Workspace.citation_style accepts 'author-year' and persists correctly."""
    from grant_lab.db.models import Workspace

    with pg_session_factory() as s:
        ws = _make_workspace(s)
        ws.citation_style = "author-year"
        s.commit()
        ws_id = ws.id

    with pg_session_factory() as s:
        loaded = s.get(Workspace, ws_id)
        assert loaded.citation_style == "author-year"


def test_citation_style_rejects_unknown_value(pg_session_factory):
    """Workspace.citation_style CHECK rejects values outside the allowed set."""
    with pg_session_factory() as s:
        ws = _make_workspace(s)
        ws.citation_style = "bogus"
        with pytest.raises(IntegrityError):
            s.commit()


def test_metadata_waiver_defaults_false(pg_session_factory):
    """Reference.metadata_waiver server_default is False."""
    with pg_session_factory() as s:
        ws = _make_workspace(s)
        ref = _make_reference(s, ws)
        s.commit()
        ref_id = ref.id

    with pg_session_factory() as s:
        from grant_lab.db.models import Reference

        loaded = s.get(Reference, ref_id)
        assert loaded is not None
        assert loaded.metadata_waiver is False
        assert loaded.metadata_waiver_reason is None


def test_metadata_waiver_true_with_reason_persists(pg_session_factory):
    """Reference.metadata_waiver=True + metadata_waiver_reason round-trip."""
    with pg_session_factory() as s:
        ws = _make_workspace(s)
        ref = _make_reference(s, ws)
        ref.metadata_waiver = True
        ref.metadata_waiver_reason = "Author confirmed verbally — no DOI available."
        s.commit()
        ref_id = ref.id

    with pg_session_factory() as s:
        from grant_lab.db.models import Reference

        loaded = s.get(Reference, ref_id)
        assert loaded.metadata_waiver is True
        assert loaded.metadata_waiver_reason == "Author confirmed verbally — no DOI available."
