"""JSON /api reference write/management endpoints (Cockpit Slice C-refs)."""

from io import BytesIO

from sqlalchemy import func, select

from call_manager.db.models import Reference, WorkspaceEvent
from call_manager.proposal_assist.references.storage import ReferenceStorage
from tests.proposal_assist._refs_helpers import (
    FakeEmbedClient,
    add_member,
    authed_client,
    build_app,
)
from tests.proposal_assist.conftest import make_user, make_workspace
from tests.proposal_assist.references.test_reprocess import _insert_failed_ref


def _real_pdf_bytes() -> bytes:
    from reportlab.pdfgen.canvas import Canvas

    buf = BytesIO()
    c = Canvas(buf)
    c.drawString(72, 720, "A real PDF paragraph for the api reference tests.")
    c.save()
    return buf.getvalue()


def _app(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    app.state.embed_client = FakeEmbedClient()
    app.state.citation_service = None  # skip ingest Phase 3
    return app


def _setup(pg_session_factory, tmp_path):
    """Return (storage, ws_id, owner_id, member_id, stranger_id). member is a viewer."""
    storage = ReferenceStorage(root=tmp_path)
    with pg_session_factory() as s:
        owner = make_user(s, email="owner@ki.se")
        member = make_user(s, email="member@ki.se")
        stranger = make_user(s, email="stranger@ki.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        add_member(s, workspace_id=ws.id, user=member)  # member becomes a viewer
        return storage, ws.id, owner.id, member.id, stranger.id


def _seed_ref(pg_session_factory, storage, ws_id, *, text_status="failed", **flip):
    """Seed one reference via the proven `_insert_failed_ref` (stores real bytes so
    reprocess/delete work). Optionally flip text_status / other fields. Returns id (str)."""
    with pg_session_factory() as s:
        rid = _insert_failed_ref(s, storage, workspace_id=ws_id, data=b"# H\n\nbody")
        if text_status != "failed" or flip:
            ref = s.get(Reference, rid)
            ref.text_status = text_status
            for k, v in flip.items():
                setattr(ref, k, v)
            s.commit()
    return str(rid)


def test_api_upload_creates_reference_and_audits(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    _, ws_id, _, member_id, _ = _setup(pg_session_factory, tmp_path)
    r = authed_client(app, user_id=member_id).post(
        f"/api/workspaces/{ws_id}/references",
        data={"kind": "paper", "auto_classify": "false"},
        files={"file": ("p.pdf", _real_pdf_bytes(), "application/pdf")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text_status"] == "done"
    rid = body["reference_id"]
    with pg_session_factory() as s:
        ev = s.scalar(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws_id,
                WorkspaceEvent.kind == "reference.added",
            )
        )
        assert ev is not None
        assert ev.payload["reference_id"] == rid


def test_api_upload_bad_mime_is_400(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    _, ws_id, _, member_id, _ = _setup(pg_session_factory, tmp_path)
    r = authed_client(app, user_id=member_id).post(
        f"/api/workspaces/{ws_id}/references",
        data={"kind": "paper", "auto_classify": "false"},
        files={"file": ("x.bin", b"data", "application/octet-stream")},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_request"


def test_api_upload_non_member_is_403(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    _, ws_id, _, _, stranger_id = _setup(pg_session_factory, tmp_path)
    r = authed_client(app, user_id=stranger_id).post(
        f"/api/workspaces/{ws_id}/references",
        data={"kind": "paper", "auto_classify": "false"},
        files={"file": ("p.pdf", _real_pdf_bytes(), "application/pdf")},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


def test_api_upload_dragdrop_dedup(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    _, ws_id, owner_id, _, _ = _setup(pg_session_factory, tmp_path)
    c = authed_client(app, user_id=owner_id)
    pdf = _real_pdf_bytes()
    for _ in range(2):
        r = c.post(
            f"/api/workspaces/{ws_id}/references",
            data={"auto_classify": "true"},
            files={"file": ("p.pdf", pdf, "application/pdf")},
        )
        assert r.status_code == 200, r.text
    with pg_session_factory() as s:
        n = s.scalar(
            select(func.count()).select_from(Reference).where(Reference.workspace_id == ws_id)
        )
    assert n == 1


def test_api_fetch_invalid_source_is_400(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    _, ws_id, owner_id, _, _ = _setup(pg_session_factory, tmp_path)
    r = authed_client(app, user_id=owner_id).post(
        f"/api/workspaces/{ws_id}/references/fetch",
        json={"input": "not a doi or url", "kind": "paper"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_request"


def test_api_fetch_success_audits(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    _, ws_id, owner_id, _, _ = _setup(pg_session_factory, tmp_path)
    pdf = _real_pdf_bytes()

    def _fake_dispatch(_input):
        return pdf, "fetched.pdf", "application/pdf", {"source": "crossref"}

    monkeypatch.setattr(
        "call_manager.proposal_assist.references.dispatcher.dispatch_fetch", _fake_dispatch
    )
    r = authed_client(app, user_id=owner_id).post(
        f"/api/workspaces/{ws_id}/references/fetch",
        json={"input": "10.1000/xyz123", "kind": "paper"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["text_status"] == "done"
    with pg_session_factory() as s:
        ev = s.scalar(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws_id, WorkspaceEvent.kind == "reference.added"
            )
        )
        assert ev is not None


def test_api_metadata_approve_sets_citekey_and_audits(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):
    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    storage, ws_id, owner_id, _, _ = _setup(pg_session_factory, tmp_path)
    rid = _seed_ref(pg_session_factory, storage, ws_id)
    r = authed_client(app, user_id=owner_id).post(
        f"/api/workspaces/{ws_id}/references/{rid}/metadata",
        json={"title": "On Widgets", "authors": ["Roe, Jane"], "year": "2021"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["metadata_status"] == "approved"
    assert body["citekey"] == "roe2021"
    assert body["csl_json"]["title"] == "On Widgets"
    with pg_session_factory() as s:
        ev = s.scalar(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws_id,
                WorkspaceEvent.kind == "reference.metadata_approved",
            )
        )
        assert ev is not None and ev.payload["reference_id"] == rid


def test_api_metadata_empty_title_is_400(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    storage, ws_id, owner_id, _, _ = _setup(pg_session_factory, tmp_path)
    rid = _seed_ref(pg_session_factory, storage, ws_id)
    r = authed_client(app, user_id=owner_id).post(
        f"/api/workspaces/{ws_id}/references/{rid}/metadata",
        json={"title": "   ", "authors": []},
    )
    assert r.status_code == 400


def test_api_metadata_unknown_ref_is_404(monkeypatch, pg_url, tmp_path, pg_session_factory):
    import uuid as _uuid

    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    _, ws_id, owner_id, _, _ = _setup(pg_session_factory, tmp_path)
    r = authed_client(app, user_id=owner_id).post(
        f"/api/workspaces/{ws_id}/references/{_uuid.uuid4()}/metadata",
        json={"title": "X", "authors": []},
    )
    assert r.status_code == 404
