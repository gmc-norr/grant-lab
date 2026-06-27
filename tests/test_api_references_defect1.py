"""Defect-1 regression tests for metadata-approval IntegrityError retry.

Three scenarios that were broken before the fix:

1. Concurrent delete after rollback → 404 not 500 (AttributeError guard).
2. Genuine citekey collision on retry → handler resolves to a NEW non-fallback
   citekey (currently the handler skips citekey regeneration on retry).
3. An unrelated IntegrityError (not the citekey unique constraint) is NOT
   swallowed — it propagates as a 500.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from call_manager.db.models import Reference
from call_manager.main import create_app
from call_manager.proposal_assist.references.storage import ReferenceStorage
from tests.proposal_assist._refs_helpers import FakeEmbedClient
from tests.proposal_assist.conftest import make_user, make_workspace
from tests.proposal_assist.references.test_reprocess import _insert_failed_ref

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _build_app(monkeypatch, pg_url, tmp_path, pg_session_factory):
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se",
        "APP_BASE_URL": "http://testserver",
        "WORKSPACES_DIR": str(tmp_path),
    }.items():
        monkeypatch.setenv(k, v)
    app = create_app()
    app.state.session_factory = pg_session_factory
    app.state.embed_client = FakeEmbedClient()
    app.state.citation_service = None
    return app


def _setup(pg_session_factory, tmp_path, *, email="defect1-owner@ki.se"):
    """Return (storage, ws_id, owner_id)."""
    storage = ReferenceStorage(root=tmp_path)
    with pg_session_factory() as s:
        owner = make_user(s, email=email)
        ws = make_workspace(s, owner=owner)
        s.commit()
        return storage, ws.id, owner.id


def _seed_ref(pg_session_factory, storage, ws_id):
    """Insert a reference row (no citekey) and return its UUID."""
    with pg_session_factory() as s:
        rid = _insert_failed_ref(s, storage, workspace_id=ws_id, data=b"# H\n\nbody")
        s.commit()
    return rid  # UUID


def _authed(client, app, user_id):
    client.cookies.set("session", app.state.session_signer.make(user_id=user_id))
    return client


# ---------------------------------------------------------------------------
# Scenario 1: concurrent delete between rollback and re-fetch → 404
# ---------------------------------------------------------------------------

def test_metadata_approve_row_gone_after_rollback_is_404(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):
    """After IntegrityError + rollback the re-fetch returns None (concurrent delete).
    The handler must return 404, not an AttributeError / 500."""
    import sqlalchemy.orm

    app = _build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    storage, ws_id, owner_id = _setup(pg_session_factory, tmp_path, email="d1s1@ki.se")
    rid = _seed_ref(pg_session_factory, storage, ws_id)

    class _FakeOrig:
        sqlstate = "23505"
        pgcode = "23505"
        pgerror = (
            "ERROR:  duplicate key value violates unique constraint "
            '"uq_references_workspace_citekey"'
        )

    with TestClient(app, raise_server_exceptions=True) as client:
        call_count = {"n": 0}
        original_commit = sqlalchemy.orm.Session.commit
        original_scalar = sqlalchemy.orm.Session.scalar

        def _patched_commit(self, *a, **kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                from sqlalchemy.exc import IntegrityError
                self.rollback()
                raise IntegrityError(
                    statement="INSERT INTO workspace_events ...",
                    params={},
                    orig=_FakeOrig(),
                )
            return original_commit(self, *a, **kw)

        def _patched_scalar(self, stmt, *a, **kw):
            # After the first commit raised, next scalar() is the retry re-fetch.
            # Simulate a concurrent delete by returning None.
            if call_count["n"] >= 1:
                return None
            return original_scalar(self, stmt, *a, **kw)

        monkeypatch.setattr(sqlalchemy.orm.Session, "commit", _patched_commit)
        monkeypatch.setattr(sqlalchemy.orm.Session, "scalar", _patched_scalar)

        r = _authed(client, app, owner_id).post(
            f"/api/workspaces/{ws_id}/references/{rid}/metadata",
            json={"title": "Widget Study", "authors": ["Roe, Jane"], "year": "2021"},
        )

    assert r.status_code == 404, f"Expected 404, got {r.status_code}: {r.text}"


# ---------------------------------------------------------------------------
# Scenario 2: genuine citekey collision → resolves to a new unique key
# ---------------------------------------------------------------------------

def test_metadata_approve_citekey_collision_regenerates_key(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):
    """When the approved citekey collides, the handler must regenerate a fresh key on
    retry. Currently, the retry block skips generate_citekey, so the response returns
    a null/fallback citekey instead of the collision-resolved variant ('roe2021a').

    The fix: on retry, if ref.citekey still matches the colliding base (or is a
    ref-fallback), regenerate. This test pins the post-fix behaviour: the response
    citekey must be a non-null, properly-formed author+year key (not the colliding one).
    """
    import sqlalchemy.orm

    app = _build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    storage, ws_id, owner_id = _setup(pg_session_factory, tmp_path, email="d1s2@ki.se")
    rid = _seed_ref(pg_session_factory, storage, ws_id)

    # Pre-seed a reference with "roe2021" to force generate_citekey to produce "roe2021a".
    with pg_session_factory() as s:
        existing = Reference(
            workspace_id=ws_id,
            kind="paper",
            source_type="uploaded",
            filename="existing.pdf",
            storage_path="x/y.pdf",
            mime_type="application/pdf",
            citekey="roe2021",
            metadata_status="approved",
        )
        s.add(existing)
        s.commit()

    class _FakeOrig:
        sqlstate = "23505"
        pgcode = "23505"
        pgerror = (
            "ERROR:  duplicate key value violates unique constraint "
            '"uq_references_workspace_citekey"'
        )

    with TestClient(app, raise_server_exceptions=True) as client:
        call_count = {"n": 0}
        original_commit = sqlalchemy.orm.Session.commit

        def _patched_commit(self, *a, **kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                from sqlalchemy.exc import IntegrityError
                self.rollback()
                raise IntegrityError(
                    statement="INSERT ...",
                    params={},
                    orig=_FakeOrig(),
                )
            return original_commit(self, *a, **kw)

        monkeypatch.setattr(sqlalchemy.orm.Session, "commit", _patched_commit)

        r = _authed(client, app, owner_id).post(
            f"/api/workspaces/{ws_id}/references/{rid}/metadata",
            json={"title": "On Widgets", "authors": ["Roe, Jane"], "year": "2021"},
        )

    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    body = r.json()
    # After fix: citekey must be 'roe2021a' (collision resolved, not the colliding base).
    # Before fix: the retry does not regenerate, leaving the ref with its old citekey.
    assert body["citekey"] == "roe2021a", (
        f"Expected collision-resolved key 'roe2021a', got: {body['citekey']!r}"
    )
    assert body["metadata_status"] == "approved"


# ---------------------------------------------------------------------------
# Scenario 3: unrelated IntegrityError is NOT swallowed
# ---------------------------------------------------------------------------

def test_metadata_approve_unrelated_integrity_error_raises(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):
    """An IntegrityError with a SQLSTATE that is NOT 23505 (e.g. 23503 FK violation)
    must NOT be swallowed by the except block — it should propagate as a 500."""
    import sqlalchemy.orm

    app = _build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    storage, ws_id, owner_id = _setup(pg_session_factory, tmp_path, email="d1s3@ki.se")
    rid = _seed_ref(pg_session_factory, storage, ws_id)

    class _FakeFKOrig:
        sqlstate = "23503"  # foreign_key_violation — NOT citekey unique
        pgcode = "23503"
        pgerror = "ERROR:  insert or update on table ... violates foreign key constraint"

    with TestClient(app, raise_server_exceptions=False) as client:
        call_count = {"n": 0}
        original_commit = sqlalchemy.orm.Session.commit

        def _patched_commit(self, *a, **kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                from sqlalchemy.exc import IntegrityError
                self.rollback()
                raise IntegrityError(
                    statement="INSERT ...",
                    params={},
                    orig=_FakeFKOrig(),
                )
            return original_commit(self, *a, **kw)

        monkeypatch.setattr(sqlalchemy.orm.Session, "commit", _patched_commit)

        r = _authed(client, app, owner_id).post(
            f"/api/workspaces/{ws_id}/references/{rid}/metadata",
            json={"title": "On Widgets", "authors": ["Roe, Jane"], "year": "2021"},
        )

    # Should be 500 (re-raised, not swallowed)
    assert r.status_code == 500, (
        f"Expected 500 for unrelated IntegrityError, got {r.status_code}: {r.text}"
    )


# ---------------------------------------------------------------------------
# Scenario 4 (R3-M2 / R-#7): DOUBLE citekey collision → clean 409, not 500
# ---------------------------------------------------------------------------

def test_metadata_approve_double_collision_returns_409_not_500(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):
    """A concurrent insert can take the regenerated suffix between our rollback
    and the RETRY commit, raising a SECOND 23505. The retry commit must be
    guarded so this double-collision returns a clean 409, not a sanitised 500."""
    import sqlalchemy.orm

    app = _build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    storage, ws_id, owner_id = _setup(pg_session_factory, tmp_path, email="d1s4@ki.se")
    rid = _seed_ref(pg_session_factory, storage, ws_id)

    class _FakeOrig:
        sqlstate = "23505"
        pgcode = "23505"
        pgerror = (
            "ERROR:  duplicate key value violates unique constraint "
            '"uq_references_workspace_citekey"'
        )

    with TestClient(app, raise_server_exceptions=False) as client:
        call_count = {"n": 0}

        def _patched_commit(self, *a, **kw):
            # BOTH the initial commit and the retry commit raise 23505.
            call_count["n"] += 1
            if call_count["n"] <= 2:
                from sqlalchemy.exc import IntegrityError
                self.rollback()
                raise IntegrityError(statement="INSERT ...", params={}, orig=_FakeOrig())
            return None

        monkeypatch.setattr(sqlalchemy.orm.Session, "commit", _patched_commit)

        r = _authed(client, app, owner_id).post(
            f"/api/workspaces/{ws_id}/references/{rid}/metadata",
            json={"title": "On Widgets", "authors": ["Roe, Jane"], "year": "2021"},
        )

    assert r.status_code == 409, (
        f"Expected clean 409 on double collision, got {r.status_code}: {r.text}"
    )
    assert call_count["n"] == 2, "both the initial and retry commits should be attempted"
