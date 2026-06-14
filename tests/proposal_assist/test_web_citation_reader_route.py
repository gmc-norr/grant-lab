"""Task 5 — GET /workspaces/{id}/citations/{ev_id}/reader

Drawer fragment: returns 200 for all expected states (found, fallback,
tombstoned, text_status!=done, missing-data), 404 only for unknown evidence_id.
"""

from __future__ import annotations

import uuid as _uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import Reference, ReferenceChunk, User, Workspace, WorkspaceViewer
from call_manager.main import create_app
from call_manager.proposal_assist.citations.ledger import (
    append_evidence,
    dump_ledger,
    empty_ledger,
)
from call_manager.proposal_assist.references.storage import ReferenceStorage
from call_manager.proposal_assist.tools.citations import CITATIONS_FILENAME
from call_manager.proposal_assist.tools.proposal import PROPOSAL_FILENAME

# ---------------------------------------------------------------------------
# App + auth helpers (same pattern as test_web_workspaces_citations.py)
# ---------------------------------------------------------------------------


def _build_app(monkeypatch, pg_url, tmp_path):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("WORKSPACES_DIR", str(tmp_path))
    return create_app()


def _authed(app, *, user_id):
    client = TestClient(app, raise_server_exceptions=True)
    client.__enter__()
    signer: SessionSigner = app.state.session_signer
    client.cookies.set("session", signer.make(user_id=user_id))
    return client


def _repo_dir(tmp_path, wsid):
    repo = tmp_path / str(wsid) / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    return repo


# ---------------------------------------------------------------------------
# Fixture factory
# ---------------------------------------------------------------------------

# Embeddings require a 1024-float vector — use a zero vector for tests.
_ZERO_EMBEDDING = [0.0] * 1024


@pytest.fixture
def reader_env(monkeypatch, pg_url, pg_session_factory, tmp_path):
    """Factory that seeds a full reader environment and returns
    ``(client, workspace_id, evidence_id)``.

    Supported opts:
      locatable=True/False  — whether evidence_quote appears in text.txt
      text_status="done"    — set to "pending" to suppress text.txt read
      tombstoned=False/True — set tombstoned_at on the Reference
      as_viewer=False/True  — authenticate as a WorkspaceViewer not the owner
      text_txt="..."        — override the extracted text content
    """
    app = _build_app(monkeypatch, pg_url, tmp_path)

    def _make(
        *,
        locatable: bool = True,
        text_status: str = "done",
        tombstoned: bool = False,
        as_viewer: bool = False,
        text_txt: str | None = None,
    ):
        ev_id = f"ev-{_uuid.uuid4().hex[:12]}"
        evidence_quote = "The treatment reduced mortality by 30 percent."

        from datetime import UTC, datetime

        owner_id = None
        viewer_id = None
        wsid = None
        ref_id = None

        with pg_session_factory() as s:
            # Owner
            owner = User(email=f"reader-owner-{_uuid.uuid4().hex[:6]}@ki.se")
            s.add(owner)
            s.flush()
            owner_id = owner.id

            # Workspace
            ws = Workspace(owner_id=owner_id, title="Reader WS")
            s.add(ws)
            s.flush()
            wsid = ws.id

            # Optional viewer
            if as_viewer:
                viewer = User(email=f"reader-viewer-{_uuid.uuid4().hex[:6]}@ki.se")
                s.add(viewer)
                s.flush()
                s.add(WorkspaceViewer(workspace_id=wsid, user_id=viewer.id))
                s.flush()
                viewer_id = viewer.id

            # Reference row
            ref = Reference(
                workspace_id=wsid,
                kind="paper",
                source_type="uploaded",
                filename="paper.pdf",
                storage_path=f"{wsid}/references/paper.pdf",
                mime_type="application/pdf",
                citekey="smith2021",
                title="Adjuvant therapy outcomes",
                text_status=text_status,
                metadata_status="approved",
                tombstoned_at=datetime(2026, 1, 1, tzinfo=UTC) if tombstoned else None,
            )
            s.add(ref)
            s.flush()
            ref_id = ref.id

            # ReferenceChunk — chunk_index=0, text contains the evidence_quote
            chunk = ReferenceChunk(
                reference_id=ref_id,
                workspace_id=wsid,
                chunk_index=0,
                section_path="Introduction",
                text=f"Some context. {evidence_quote} More context.",
                embedding=_ZERO_EMBEDDING,
                embedding_model="test-embed",
            )
            s.add(chunk)
            s.commit()

        # Write proposal.md (one marker)
        repo = _repo_dir(tmp_path, wsid)
        (repo / PROPOSAL_FILENAME).write_text(
            f"[@smith2021]<!-- ev:{ev_id} --> is our key finding.\n"
        )

        # Write citations.json
        row = {
            "id": ev_id,
            "citekey": "smith2021",
            "reference_id": str(ref_id),
            "locator": {"chunk_index": 0, "section_path": "Introduction"},
            "source_content_hash": None,
            "chunk_content_hash": "deadbeef",
            "evidence_quote": evidence_quote,
            "evidence_quote_offsets": [0, len(evidence_quote)],
            "stance": "supports",
            "confidence": "high",
            "claim_context": {"text": "Our approach lowers mortality.", "hash": "cafef00d"},
            "citation_snapshot": "Smith 2021",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
        ledger = append_evidence(empty_ledger(), row)
        (repo / CITATIONS_FILENAME).write_text(dump_ledger(ledger))

        # Write text.txt via ReferenceStorage (only when text_status == "done")
        storage = ReferenceStorage(str(tmp_path))
        if text_status == "done":
            if text_txt is not None:
                content = text_txt
            elif locatable:
                # Quote is present so locate_evidence finds it
                content = f"Introduction.\n{evidence_quote}\nConclusion."
            else:
                # Quote absent — locate_evidence returns None → chunk fallback
                content = "Introduction.\nSome unrelated text here.\nConclusion."
            # write_text requires the directory to exist (normally created by write_atomic)
            text_p = storage.text_path_for(workspace_id=wsid, reference_id=ref_id)
            text_p.parent.mkdir(parents=True, exist_ok=True)
            text_p.write_text(content, encoding="utf-8")

        # Authenticate as owner or viewer
        act_user_id = viewer_id if as_viewer else owner_id
        client = _authed(app, user_id=act_user_id)
        return client, wsid, ev_id

    yield _make


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_reader_returns_fragment_with_highlight(reader_env):
    client, wid, ev = reader_env(locatable=True)
    r = client.get(f"/workspaces/{wid}/citations/{ev}/reader")
    assert r.status_code == 200
    assert 'id="ev-target"' in r.text and "<mark" in r.text
    assert 'role="dialog"' in r.text
    assert "SUPPORTS" in r.text.upper()


def test_unknown_evidence_id_is_404(reader_env):
    client, wid, _ = reader_env(locatable=True)
    assert client.get(f"/workspaces/{wid}/citations/ev-deadbeef99/reader").status_code == 404


def test_no_text_status_done_shows_notice_200(reader_env):
    client, wid, ev = reader_env(text_status="pending")
    r = client.get(f"/workspaces/{wid}/citations/{ev}/reader")
    assert r.status_code == 200 and "not available" in r.text.lower()


def test_tombstoned_reference_renders_with_hint_200(reader_env):
    client, wid, ev = reader_env(tombstoned=True)
    r = client.get(f"/workspaces/{wid}/citations/{ev}/reader")
    assert r.status_code == 200 and "removed from the library" in r.text.lower()


def test_unlocatable_quote_falls_back_to_chunk_200(reader_env):
    client, wid, ev = reader_env(locatable=False)
    r = client.get(f"/workspaces/{wid}/citations/{ev}/reader")
    assert r.status_code == 200
    assert "couldn't pinpoint" in r.text.lower() or "cited section" in r.text.lower()


def test_viewer_can_read(reader_env):
    client, wid, ev = reader_env(as_viewer=True)
    assert client.get(f"/workspaces/{wid}/citations/{ev}/reader").status_code == 200


def test_reference_text_is_html_escaped(reader_env):
    # text_txt must contain the evidence_quote so locate_evidence can find it;
    # the XSS payload in context should be HTML-escaped in the rendered body.
    xss_prefix = "danger <script>alert(1)</script> ok. "
    evidence_quote = "The treatment reduced mortality by 30 percent."
    client, wid, ev = reader_env(
        text_txt=f"{xss_prefix}{evidence_quote} More text here."
    )
    r = client.get(f"/workspaces/{wid}/citations/{ev}/reader")
    assert r.status_code == 200
    assert "<script>alert" not in r.text and "&lt;script&gt;" in r.text


def test_corrupt_ledger_returns_degraded_200(reader_env):
    """A corrupt citations.json must yield a 200 degraded fragment, not a 500."""
    client, wid, ev = reader_env(locatable=True)
    # Locate and overwrite the ledger with malformed JSON
    workspaces_dir = client.app.state.settings.workspaces_dir
    citations_path = Path(workspaces_dir) / str(wid) / "repo" / CITATIONS_FILENAME
    citations_path.write_text("{ not json", encoding="utf-8")
    r = client.get(f"/workspaces/{wid}/citations/{ev}/reader")
    assert r.status_code == 200
    assert "unavailable" in r.text.lower()


def test_cross_workspace_reference_boundary(reader_env, pg_session_factory, tmp_path):
    """A reference belonging to workspace B must not be readable via workspace A's reader."""
    # Set up workspace A with a normal env (ev_id, wid from reader_env)
    client, wid_a, ev_id = reader_env(locatable=True)

    # Seed workspace B with a foreign reference containing unique identifying text
    foreign_unique_text = "FOREIGN_SECRET_CONTENT_XYZ_99999"
    foreign_ref_id = None
    with pg_session_factory() as s:
        owner_b = User(email=f"owner-b-{_uuid.uuid4().hex[:6]}@ki.se")
        s.add(owner_b)
        s.flush()
        ws_b = Workspace(owner_id=owner_b.id, title="Workspace B")
        s.add(ws_b)
        s.flush()
        ref_b = Reference(
            workspace_id=ws_b.id,
            kind="paper",
            source_type="uploaded",
            filename="foreign.pdf",
            storage_path=f"{ws_b.id}/references/foreign.pdf",
            mime_type="application/pdf",
            citekey="foreign2021",
            title=foreign_unique_text,
            text_status="done",
            metadata_status="approved",
            tombstoned_at=None,
        )
        s.add(ref_b)
        s.commit()
        foreign_ref_id = ref_b.id

    # Overwrite workspace A's citations.json so its evidence row points to the
    # foreign reference id (from workspace B)
    workspaces_dir = client.app.state.settings.workspaces_dir
    citations_path = Path(workspaces_dir) / str(wid_a) / "repo" / CITATIONS_FILENAME
    evidence_quote = "The treatment reduced mortality by 30 percent."
    row = {
        "id": ev_id,
        "citekey": "smith2021",
        "reference_id": str(foreign_ref_id),  # <-- foreign workspace's ref
        "locator": {"chunk_index": 0, "section_path": "Introduction"},
        "source_content_hash": None,
        "chunk_content_hash": "deadbeef",
        "evidence_quote": evidence_quote,
        "evidence_quote_offsets": [0, len(evidence_quote)],
        "stance": "supports",
        "confidence": "high",
        "claim_context": {"text": "Our approach lowers mortality.", "hash": "cafef00d"},
        "citation_snapshot": "Smith 2021",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    ledger = append_evidence(empty_ledger(), row)
    citations_path.write_text(dump_ledger(ledger), encoding="utf-8")

    r = client.get(f"/workspaces/{wid_a}/citations/{ev_id}/reader")
    assert r.status_code == 200
    assert "unavailable" in r.text.lower()
    assert foreign_unique_text not in r.text
