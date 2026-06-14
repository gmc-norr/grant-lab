"""Task 10 — auto_classify PDF magic-byte sniff + same-hash live dedup."""

from io import BytesIO

from sqlalchemy import func, select

from call_manager.db.models import Reference
from tests.proposal_assist._refs_helpers import FakeEmbedClient, authed_client, build_app
from tests.proposal_assist.conftest import make_workspace


def _real_pdf_bytes() -> bytes:
    """Generate a minimal one-page PDF using reportlab (test-time dependency).

    Pattern taken from tests/proposal_assist/references/test_extract.py
    so pdfminer can parse it and text_status reaches 'done'.
    """
    from reportlab.pdfgen.canvas import Canvas

    buf = BytesIO()
    c = Canvas(buf)
    c.drawString(72, 720, "A real PDF paragraph for dedup testing.")
    c.save()
    return buf.getvalue()


def test_auto_classify_rejects_non_pdf_bytes(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid, owner_id = ws.id, ws.owner_id
    resp = authed_client(app, user_id=owner_id).post(
        f"/workspaces/{wsid}/references",
        data={"auto_classify": "true"},
        files={"file": ("fake.pdf", b"not a pdf", "application/pdf")},
    )
    assert resp.status_code == 400
    assert "PDF" in resp.text


def test_duplicate_done_pdf_skips_reingest(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    app.state.embed_client = FakeEmbedClient()
    app.state.citation_service = None  # skip Phase 3
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid, owner_id = ws.id, ws.owner_id
    client = authed_client(app, user_id=owner_id)
    pdf = _real_pdf_bytes()
    r1 = client.post(
        f"/workspaces/{wsid}/references",
        data={"auto_classify": "true"},
        files={"file": ("p.pdf", pdf, "application/pdf")},
    )
    assert r1.status_code == 200, r1.text
    r2 = client.post(
        f"/workspaces/{wsid}/references",
        data={"auto_classify": "true"},
        files={"file": ("p.pdf", pdf, "application/pdf")},
    )
    assert r2.status_code == 200
    with pg_session_factory() as s:
        count = s.scalar(
            select(func.count()).select_from(Reference).where(Reference.workspace_id == wsid)
        )
    assert count == 1  # dedup: no second row created


def test_redrop_of_failed_ref_does_not_duplicate(monkeypatch, pg_url, tmp_path, pg_session_factory):
    """A re-dropped file matching a FAILED (non-tombstoned) row returns that
    row rather than creating a duplicate — the subtle dedup property: dedup
    matches ANY live same-hash row, not just 'done' ones."""
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    app.state.embed_client = FakeEmbedClient()
    app.state.citation_service = None
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid, owner_id = ws.id, ws.owner_id
    client = authed_client(app, user_id=owner_id)
    pdf = _real_pdf_bytes()
    r1 = client.post(
        f"/workspaces/{wsid}/references",
        data={"auto_classify": "true"},
        files={"file": ("p.pdf", pdf, "application/pdf")},
    )
    assert r1.status_code == 200, r1.text
    # Simulate a retained ingestion failure on that row.
    with pg_session_factory() as s:
        ref = s.scalar(select(Reference).where(Reference.workspace_id == wsid))
        ref.text_status = "failed"
        ref.text_error = "simulated"
        s.commit()
    # Re-drop the identical bytes: must return the existing (failed) row, not a dup.
    r2 = client.post(
        f"/workspaces/{wsid}/references",
        data={"auto_classify": "true"},
        files={"file": ("p.pdf", pdf, "application/pdf")},
    )
    assert r2.status_code == 200, r2.text
    with pg_session_factory() as s:
        count = s.scalar(
            select(func.count()).select_from(Reference).where(Reference.workspace_id == wsid)
        )
        statuses = list(
            s.scalars(select(Reference.text_status).where(Reference.workspace_id == wsid))
        )
    assert count == 1  # no duplicate row
    assert statuses == ["failed"]  # still the original failed row (not re-ingested)


def test_single_file_picker_does_not_dedup(monkeypatch, pg_url, tmp_path, pg_session_factory):
    """Dedup is scoped to the drag-drop (auto_classify) path. The single-file
    picker must NOT dedup, so re-uploading identical bytes with a different kind
    creates a distinct row and honours the explicitly chosen kind."""
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    app.state.embed_client = FakeEmbedClient()
    app.state.citation_service = None
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid, owner_id = ws.id, ws.owner_id
    client = authed_client(app, user_id=owner_id)
    pdf = _real_pdf_bytes()
    r1 = client.post(
        f"/workspaces/{wsid}/references",
        data={"kind": "paper"},
        files={"file": ("p.pdf", pdf, "application/pdf")},
    )
    assert r1.status_code == 200, r1.text
    r2 = client.post(
        f"/workspaces/{wsid}/references",
        data={"kind": "data"},
        files={"file": ("p.pdf", pdf, "application/pdf")},
    )
    assert r2.status_code == 200, r2.text
    with pg_session_factory() as s:
        kinds = sorted(
            s.scalars(select(Reference.kind).where(Reference.workspace_id == wsid))
        )
    assert kinds == ["data", "paper"]  # two distinct rows, explicit kinds preserved
