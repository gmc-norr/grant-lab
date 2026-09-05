"""JSON /api reference write/management endpoints (Cockpit Slice C-refs)."""

from io import BytesIO

from sqlalchemy import func, select

from grant_lab.db.models import Reference, WorkspaceEvent
from grant_lab.proposal_assist.references.storage import ReferenceStorage
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
        owner = make_user(s, email="owner@umu.se")
        member = make_user(s, email="member@umu.se")
        stranger = make_user(s, email="stranger@umu.se")
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


def test_api_upload_acquires_slot_before_reading(monkeypatch, pg_url, tmp_path, pg_session_factory):
    """Over-capacity upload returns 429 WITHOUT buffering the file: the limiter
    slot is acquired before _read_capped runs (parity with the Jinja path)."""
    import grant_lab.web.api.references as refmod
    from grant_lab.proposal_assist.references.admission import IngestLimiter

    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    _, ws_id, owner_id, _, _ = _setup(pg_session_factory, tmp_path)

    lim = IngestLimiter(capacity=1)
    assert lim.try_acquire()  # hold the only slot so the request is over capacity
    app.state.reference_ingest_limiter = lim

    reads = {"n": 0}
    real_read = refmod._read_capped

    async def _spy_read(upload, max_bytes):
        reads["n"] += 1
        return await real_read(upload, max_bytes)

    monkeypatch.setattr(refmod, "_read_capped", _spy_read)

    r = authed_client(app, user_id=owner_id).post(
        f"/api/workspaces/{ws_id}/references",
        data={"kind": "paper", "auto_classify": "false"},
        files={"file": ("p.pdf", _real_pdf_bytes(), "application/pdf")},
    )
    assert r.status_code == 429
    assert r.headers.get("Retry-After") == "2"
    assert reads["n"] == 0  # slot rejected the request before the file was read


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
        "grant_lab.proposal_assist.references.dispatcher.dispatch_fetch", _fake_dispatch
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


def test_api_waiver_on_then_off_audits(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    storage, ws_id, owner_id, _, _ = _setup(pg_session_factory, tmp_path)
    rid = _seed_ref(pg_session_factory, storage, ws_id)
    c = authed_client(app, user_id=owner_id)

    r1 = c.post(
        f"/api/workspaces/{ws_id}/references/{rid}/waiver",
        json={"waiver": True, "reason": "preprint"},
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["metadata_waiver"] is True
    assert r1.json()["reason"] == "preprint"

    r2 = c.post(f"/api/workspaces/{ws_id}/references/{rid}/waiver", json={"waiver": False})
    assert r2.status_code == 200
    assert r2.json()["metadata_waiver"] is False
    assert r2.json()["reason"] is None

    with pg_session_factory() as s:
        kinds = set(
            s.scalars(select(WorkspaceEvent.kind).where(WorkspaceEvent.workspace_id == ws_id)).all()
        )
    assert "reference_metadata_waived" in kinds
    assert "reference_metadata_waiver_cleared" in kinds


def test_api_update_kind_audits_and_validates(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    storage, ws_id, owner_id, _, _ = _setup(pg_session_factory, tmp_path)
    rid = _seed_ref(pg_session_factory, storage, ws_id, kind="paper")
    c = authed_client(app, user_id=owner_id)

    ok = c.patch(f"/api/workspaces/{ws_id}/references/{rid}/kind", json={"kind": "data"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["kind"] == "data"

    bad = c.patch(f"/api/workspaces/{ws_id}/references/{rid}/kind", json={"kind": "nope"})
    assert bad.status_code == 400

    with pg_session_factory() as s:
        ev = s.scalar(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws_id,
                WorkspaceEvent.kind == "reference.kind_changed",
            )
        )
        assert ev is not None and ev.payload["to"] == "data"


def test_api_delete_uncited_hard_deletes(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    storage, ws_id, owner_id, _, _ = _setup(pg_session_factory, tmp_path)
    rid = _seed_ref(pg_session_factory, storage, ws_id)  # fresh, uncited → hard delete
    r = authed_client(app, user_id=owner_id).post(
        f"/api/workspaces/{ws_id}/references/{rid}/delete"
    )
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "deleted"
    with pg_session_factory() as s:
        assert s.get(Reference, rid) is None
        ev = s.scalar(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws_id, WorkspaceEvent.kind == "reference.deleted"
            )
        )
        assert ev is not None


def test_api_reprocess_conflict_when_not_failed(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    storage, ws_id, owner_id, _, _ = _setup(pg_session_factory, tmp_path)
    rid = _seed_ref(pg_session_factory, storage, ws_id, text_status="done")
    r = authed_client(app, user_id=owner_id).post(
        f"/api/workspaces/{ws_id}/references/{rid}/reprocess"
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"


def test_api_reprocess_failed_ref_audits(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    storage, ws_id, owner_id, _, _ = _setup(pg_session_factory, tmp_path)
    # _seed_ref stores real bytes via _insert_failed_ref and leaves the ref in
    # 'failed', so reprocess re-extracts → 'done' (FakeEmbedClient; Phase 3 skipped).
    rid = _seed_ref(pg_session_factory, storage, ws_id)
    r = authed_client(app, user_id=owner_id).post(
        f"/api/workspaces/{ws_id}/references/{rid}/reprocess"
    )
    assert r.status_code == 200, r.text
    assert r.json()["text_status"] == "done"
    with pg_session_factory() as s:
        ev = s.scalar(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws_id, WorkspaceEvent.kind == "reference.reprocessed"
            )
        )
        assert ev is not None


def test_api_serve_reference_raw_member_roundtrips_bytes(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):
    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    _, ws_id, _, member_id, _ = _setup(pg_session_factory, tmp_path)
    pdf = _real_pdf_bytes()
    rid = authed_client(app, user_id=member_id).post(
        f"/api/workspaces/{ws_id}/references",
        data={"kind": "paper", "auto_classify": "false"},
        files={"file": ("p.pdf", pdf, "application/pdf")},
    ).json()["reference_id"]
    r = authed_client(app, user_id=member_id).get(
        f"/api/workspaces/{ws_id}/references/{rid}/raw"
    )
    assert r.status_code == 200, r.text
    assert r.content == pdf
    assert r.headers["content-disposition"].startswith("attachment;")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "private, max-age=300"


def test_api_serve_reference_raw_non_member_403(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):
    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    _, ws_id, _, member_id, stranger_id = _setup(pg_session_factory, tmp_path)
    rid = authed_client(app, user_id=member_id).post(
        f"/api/workspaces/{ws_id}/references",
        data={"kind": "paper", "auto_classify": "false"},
        files={"file": ("p.pdf", _real_pdf_bytes(), "application/pdf")},
    ).json()["reference_id"]
    r = authed_client(app, user_id=stranger_id).get(
        f"/api/workspaces/{ws_id}/references/{rid}/raw"
    )
    assert r.status_code == 403, r.text


def test_api_serve_reference_raw_missing_reference_404(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):
    import uuid

    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    _, ws_id, _, member_id, _ = _setup(pg_session_factory, tmp_path)
    r = authed_client(app, user_id=member_id).get(
        f"/api/workspaces/{ws_id}/references/{uuid.uuid4()}/raw"
    )
    assert r.status_code == 404, r.text


def test_api_serve_reference_raw_missing_bytes_404(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):
    import uuid

    from grant_lab.proposal_assist.references.ingest import suffix_for_mime

    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    storage, ws_id, _, member_id, _ = _setup(pg_session_factory, tmp_path)
    rid = authed_client(app, user_id=member_id).post(
        f"/api/workspaces/{ws_id}/references",
        data={"kind": "paper", "auto_classify": "false"},
        files={"file": ("p.pdf", _real_pdf_bytes(), "application/pdf")},
    ).json()["reference_id"]
    # DB row remains, stored bytes removed -> serve must 404, not 500. Derive the
    # suffix exactly as the serve does so the unlink can't miss the file.
    storage.path_for(
        workspace_id=ws_id,
        reference_id=uuid.UUID(rid),
        suffix=suffix_for_mime("application/pdf"),
    ).unlink()
    r = authed_client(app, user_id=member_id).get(
        f"/api/workspaces/{ws_id}/references/{rid}/raw"
    )
    assert r.status_code == 404, r.text


def test_api_serve_reference_raw_cross_workspace_404(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):
    """IDOR: a member of workspace A cannot read a reference owned by workspace B."""
    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    _, ws_a, _, member_a, _ = _setup(pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        owner_b = make_user(s, email="ownerb@umu.se")
        ws_b = make_workspace(s, owner=owner_b)
        s.commit()
        ws_b_id, owner_b_id = ws_b.id, owner_b.id
    rid_b = authed_client(app, user_id=owner_b_id).post(
        f"/api/workspaces/{ws_b_id}/references",
        data={"kind": "paper", "auto_classify": "false"},
        files={"file": ("p.pdf", _real_pdf_bytes(), "application/pdf")},
    ).json()["reference_id"]
    # member of A requests B's reference id under A's URL -> 404 (not 403, not 200).
    r = authed_client(app, user_id=member_a).get(
        f"/api/workspaces/{ws_a}/references/{rid_b}/raw"
    )
    assert r.status_code == 404, r.text
