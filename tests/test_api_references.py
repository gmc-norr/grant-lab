"""JSON /api reference write/management endpoints (Cockpit Slice C-refs)."""

from io import BytesIO

import pytest
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


def test_api_fetch_ssrf_refusal_hides_the_internal_address(
    monkeypatch, pg_url, tmp_path, pg_session_factory, caplog
):
    """review 2026-10-26 M-sec-1: the refusal was an internal-hostname and IP
    oracle for anyone logged in.

    A7's probe, verbatim:

        http://pacs.hospital.internal/x.pdf
          -> 400 "refused to connect to 'pacs.hospital.internal' → private IP '10.20.30.40'"

    Any active user can create a workspace, so "member" means any logged-in
    user — which made this a network-mapping tool. The member now gets one
    generic sentence, and the detail goes to the server log, where it is still
    debuggable. Both halves are asserted: a refusal that told nobody anything
    would be undebuggable, which is its own defect.
    """
    import logging
    import socket

    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    _, ws_id, owner_id, _, _ = _setup(pg_session_factory, tmp_path)

    hostname = "pacs.hospital.internal"
    internal_ip = "10.20.30.40"
    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host, *args, **kwargs):
        if host == hostname:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (internal_ip, 0))]
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    with caplog.at_level(logging.WARNING, logger="grant_lab.web.api.references"):
        r = authed_client(app, user_id=owner_id).post(
            f"/api/workspaces/{ws_id}/references/fetch",
            json={"input": f"http://{hostname}/x.pdf", "kind": "paper"},
        )

    assert r.status_code == 400, r.text
    assert r.json()["error"]["message"] == "That address isn't allowed."
    # The oracle, closed: not the resolved IP, not the phrase that named it,
    # and not the hostname the caller was probing for.
    assert internal_ip not in r.text
    assert "private IP" not in r.text
    assert hostname not in r.text
    # ...and still debuggable server-side.
    assert internal_ip in caplog.text
    assert hostname in caplog.text


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


def test_api_delete_on_an_unreadable_worktree_is_a_tombstone_not_a_500(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):
    """The member-facing half of M3 (review 2026-10-26 final).

    `_worktree_cites` fails closed by RAISING when the working tree cannot be
    stat-ed, and `api_delete_reference` catches only `ValueError` — so the member
    got `500 internal server error` on a path that is safe and has a correct
    answer. The route must report the tombstone it would report for any other
    unreadable-ledger case."""
    from pathlib import Path as _Path

    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    storage, ws_id, owner_id, _, _ = _setup(pg_session_factory, tmp_path)
    rid = _seed_ref(pg_session_factory, storage, ws_id)  # uncited: a hard delete, but for this
    repo_dir = tmp_path / str(ws_id) / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)

    real_exists = _Path.exists

    def _unreadable_worktree(self, *a, **kw):
        if self == repo_dir / "citations.json":
            raise PermissionError(13, "Permission denied")
        return real_exists(self, *a, **kw)

    monkeypatch.setattr(_Path, "exists", _unreadable_worktree)

    r = authed_client(app, user_id=owner_id).post(
        f"/api/workspaces/{ws_id}/references/{rid}/delete"
    )
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "tombstoned"
    # POSITIVE CONTROL that the reference really was the uncited one a readable
    # worktree would have HARD-DELETED (test_api_delete_uncited_hard_deletes
    # above): the row survives here, so the tombstone came from the guard and not
    # from the reference being cited.
    with pg_session_factory() as s:
        ref = s.get(Reference, rid)
        assert ref is not None and ref.tombstoned_at is not None


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


# ---------------------------------------------------------------------------
# review 2026-10-26 M-sec-1, remaining half: the STATUS CODE was the oracle
# ---------------------------------------------------------------------------


@pytest.fixture
def local_reference_server():
    """A real HTTP server on 127.0.0.1 and an EPHEMERAL port (port 0, then read
    back what the OS assigned — never a fixed port, so this cannot collide with
    the dev app on :8000/:5173 or with a parallel xdist worker).

    Two routes, because the positive controls need both:
      * ``/ok.pdf``   -> 200 + a real PDF, so a fetchable URL still ingests;
      * anything else -> a genuine HTTP 500 from a reachable host.

    Yields the port. Teardown runs in a `finally`, so a failing test cannot
    leave the listener behind.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    pdf = _real_pdf_bytes()

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_GET(self):  # noqa: N802  (stdlib handler naming)
            if self.path == "/ok.pdf":
                body, status, ctype = pdf, 200, "application/pdf"
            else:
                body, status, ctype = b"upstream exploded", 500, "text/plain"
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass  # keep pytest's output clean

    # Constructed BEFORE the test body installs its getaddrinfo stub: binding
    # calls socket.getfqdn(), and a stubbed resolver has no business answering
    # the server's own bind.
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_api_fetch_hides_whether_an_internal_name_exists(
    monkeypatch, pg_url, tmp_path, pg_session_factory, caplog, local_reference_server
):
    """M-sec-1 again, through the status code.

    Task 20 made a name that resolves to an internal address answer
    ``400 "That address isn't allowed."`` — but a name that does NOT resolve
    still answered ``502 "fetch failed: DNS resolution failed for
    'pacs.hospital.internal': ..."``. So a 400 uniquely meant "this name
    resolves to an address inside your network", which is the hostname half of
    the same question, and the resolver error echoed the name on top.

    The two answers are compared to EACH OTHER rather than to two hardcoded
    expectations, so a later change that moves one of them has to move both.
    The shared answer is then pinned, because two identical 500s would satisfy
    an equality check on its own.

    Positive controls, through the REAL fetch path against a real local server:
    a fetchable URL still returns 200, and a reachable host answering HTTP 500
    still returns 502 WITH its detail. Without them, "refuse everything the
    same way" would pass — and make every fetch failure look like a policy
    refusal, which is undebuggable for the member.
    """
    import logging
    import socket

    from grant_lab import net_safety
    from grant_lab.net_safety import SSRF_REFUSAL_MESSAGE

    app = _app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    _, ws_id, owner_id, _, _ = _setup(pg_session_factory, tmp_path)

    port = local_reference_server
    blocked_host = "pacs.hospital.internal"
    internal_ip = "10.20.30.40"
    missing_host = "nowhere.hospital.internal"
    public_host = "reachable.hospital.test"

    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host, *args, **kwargs):
        if host == blocked_host:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (internal_ip, 0))]
        if host == missing_host:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        if host == public_host:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    # A local server cannot listen on a public address, so the ONE seam the
    # resolver consults is relaxed for 127.0.0.1 only. Every other address —
    # 10.20.30.40 included — keeps the production answer.
    real_is_private_ip = net_safety.is_private_ip
    monkeypatch.setattr(
        net_safety,
        "is_private_ip",
        lambda ip_str: False if ip_str == "127.0.0.1" else real_is_private_ip(ip_str),
    )

    client = authed_client(app, user_id=owner_id)

    def _fetch(url):
        return client.post(
            f"/api/workspaces/{ws_id}/references/fetch",
            json={"input": url, "kind": "paper"},
        )

    with caplog.at_level(logging.WARNING, logger="grant_lab.web.api.references"):
        r_blocked = _fetch(f"http://{blocked_host}/x.pdf")
        r_missing = _fetch(f"http://{missing_host}/x.pdf")
        # Positive control 1: a genuinely fetchable public URL still succeeds.
        r_ok = _fetch(f"http://{public_host}:{port}/ok.pdf")
        # Positive control 2: a REACHABLE host that fails upstream.
        r_upstream = _fetch(f"http://{public_host}:{port}/boom.pdf")

    # The oracle, closed: the two answers are the same answer.
    assert r_blocked.status_code == r_missing.status_code, (
        r_blocked.status_code,
        r_missing.status_code,
    )
    assert r_blocked.content == r_missing.content, (r_blocked.text, r_missing.text)
    # ...and that shared answer is the generic refusal, not two matching 500s.
    assert r_blocked.status_code == 400, r_blocked.text
    assert r_blocked.json()["error"]["message"] == SSRF_REFUSAL_MESSAGE
    for r in (r_blocked, r_missing):
        assert internal_ip not in r.text
        assert blocked_host not in r.text
        assert missing_host not in r.text
        assert "private IP" not in r.text
        assert "DNS resolution failed" not in r.text
    # Both details are still in the log.
    assert internal_ip in caplog.text
    assert blocked_host in caplog.text
    assert missing_host in caplog.text

    # Positive control 1: a genuinely fetchable public URL still succeeds, all
    # the way through ingest.
    assert r_ok.status_code == 200, r_ok.text
    assert r_ok.json()["text_status"] == "done", r_ok.text

    # Positive control 2: a REACHABLE host that fails upstream is still a
    # DIFFERENT answer — a policy refusal and a broken server must not look
    # alike, or the member can never tell which one they hit and the feature is
    # undebuggable.
    assert r_upstream.status_code == 502, r_upstream.text
    assert r_upstream.content != r_blocked.content
    assert r_upstream.json()["error"]["message"] != SSRF_REFUSAL_MESSAGE
    # Measured while writing this test: `main.py`'s /api handler rewrites EVERY
    # 5xx body to "internal server error", so the `fetch failed: …` detail the
    # review read as the leak never reached the wire at all — the STATUS CODE
    # was the entire oracle. Which also means the 502's reason now reaches
    # nothing unless the route logs it, so it does.
    assert r_upstream.json()["error"]["message"] == "internal server error"
    assert "HTTP 500" in caplog.text
    assert "upstream exploded" in caplog.text
